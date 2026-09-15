# Maintenance and Operations

Routine upkeep of a running OpenScientist deployment.

Each section separates two things:

- **Mechanism** — what the repository actually implements today, verifiable in code or configuration.
- **Recommended practice** — sensible operational advice that is *not* automated. Do not read these as existing functionality.

Related: [`DEPLOYMENT.md`](DEPLOYMENT.md), [`ENVIRONMENTS.md`](ENVIRONMENTS.md), [`CICD.md`](CICD.md), [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md).

## Dependency Maintenance

**Mechanism.** Dependabot (`.github/dependabot.yml`) opens weekly update PRs for pip (limit 10), Docker (limit 5), and GitHub Actions. Each PR runs the full CI suite. Two CI jobs gate dependency risk on every PR: `pip-audit --local --desc` against installed packages, and `dependency-review-action` failing on high or critical severity. Locally, dependencies are managed with `uv sync` against the committed `uv.lock`.

**Recommended practice.** Review Dependabot PRs on a regular cadence rather than letting them accumulate — a large backlog makes it hard to tell a routine bump from a security-relevant one. Treat a `pip-audit` failure on `main` as an incident rather than a normal red build.

## Security Maintenance

**Mechanism.** Secret scanning runs in two places: the gitleaks pre-commit hook and the CI `secret-scan` job over the PR commit range. Dockerfiles are linted by hadolint at `error` threshold. Standing findings with remediation status are tracked in [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md).

**Recommended practice.** Re-run the security review periodically and after significant architectural change, updating the status of each finding rather than appending a new document. Unresolved findings in that document — notably the absence of an audit log for admin actions, missing CSRF tokens, unpinned base image digests, and the lack of per-iteration timeouts or token budgets — remain open work items.

## Application and Image Upgrades

**Mechanism.** Deployed environments are upgraded by their pipeline: a push to `development`, `staging`, or `main` builds `openscientist:<env>-<sha>` in Azure Container Registry and redeploys only the `openscientist` web service on the target VM. Locally and on manually managed hosts, `make build` builds all four images (`openscientist-base`, main app, `openscientist-executor`, `openscientist-agent`) and `make rebuild` rebuilds and restarts.

The agent and executor images used for spawned containers are selected by `OPENSCIENTIST_AGENT_IMAGE` and `OPENSCIENTIST_EXECUTOR_IMAGE`.

**Recommended practice.** The deploy pipeline replaces only the web image. Agent and executor images are pulled by tag at job launch, so if those tags are mutable (`:latest`), a job may pick up a different build than the web app was released against. Prefer pinning agent and executor images to explicit tags per environment and updating them deliberately.

## Database Migrations

**Mechanism.** Schema changes are forward-only Alembic revisions in `src/openscientist/database/migrations/versions/`. Applied migration history is immutable — revisions are never edited, split, squashed, or rewritten once merged and applied. The current chain and a section map for the large initial migration are in the [migrations README](../src/openscientist/database/migrations/README.md).

Apply migrations against a running stack:

```bash
docker compose exec openscientist alembic upgrade head
```

Create a new revision from the current head:

```bash
uv run alembic revision --autogenerate -m "Description of changes"
```

The application does **not** apply migrations at startup. It does verify at startup that Row-Level Security is enabled and forced on the `jobs` table and that the `openscientist_app` role exists without `SUPERUSER` or `BYPASSRLS`, logging errors that name `alembic upgrade head` when those checks fail.

`POSTGRES_PASSWORD` must be set when applying the initial migration, because `init_full_schema` creates the `openscientist_admin` role using it.

> **Engineering follow-up.** The deployment workflows do not run migrations, while the SSH-based `make deploy` target does. Until reconciled, a release with a schema change requires an operator to run `alembic upgrade head` on the target VM after deployment. See [`CICD.md`](CICD.md).

**Recommended practice.** Prefer backwards-compatible migrations, so that rolling the image back to the previous tag does not require a schema downgrade. Apply and verify a migration on staging before the same release reaches production. Downgrades exist (`alembic downgrade -1`) but downgrading through `init_full_schema` drops the entire schema, so treat downgrade as a last resort rather than a rollback strategy.

## Backup and Restore

**Mechanism.** Two distinct stores must both be captured:

- **PostgreSQL** — the system of record for users, sessions, jobs, findings, hypotheses, literature, shares, API keys, cost records, and skills. Under Compose it lives in the `postgres_data` named volume.
- **Job artifacts on disk** — `./jobs` (job data, transcripts, iteration logs, generated figures, reports) and `./data` (uploaded files).

Archiving job artifacts:

```bash
tar -czf openscientist-jobs-backup-$(date +%Y%m%d).tar.gz jobs/
tar -xzf openscientist-jobs-backup-YYYYMMDD.tar.gz
```

**Recommended practice.** The repository provides no database backup automation. Add a scheduled `pg_dump` (or a volume/disk snapshot) per environment, and back up the database and job artifacts together so they can be restored to a consistent point — restoring one without the other leaves job rows pointing at missing artifacts, or artifacts with no owning rows. Test a restore periodically; an untested backup is not a backup.

## Secrets and Key Rotation

**Mechanism.** `OPENSCIENTIST_SECRET_KEY` is the master secret. `Settings.derive_secrets()` derives `storage_secret` and `token_encryption_key` from it via HMAC-SHA256, so the derived keys are not stored separately. OAuth tokens are encrypted at rest with Fernet; API key secrets are stored as SHA-256 hashes and shown only once at creation.

Provider credentials, database URLs, and OAuth client secrets are supplied per environment through that environment's `.env`. Deployment credentials are GitHub Actions secrets (`AZURE_CREDENTIALS`, `ACR_NAME`); registry pull tokens are minted per deploy run via `az acr login --expose-token`.

**Recommended practice.** There is no graceful key-rotation workflow: changing `OPENSCIENTIST_SECRET_KEY` invalidates existing sessions and makes previously encrypted OAuth tokens unreadable (recorded as an open finding in [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md)). Plan rotation as a maintenance window in which users re-authenticate, use a distinct value per environment, and rotate provider credentials and OAuth client secrets on their own schedule through the provider consoles. If a secret is exposed, rotate it rather than only removing it from history.

## Job and Container Hygiene

**Mechanism.** Job records and directories can be pruned through the job CLI:

```bash
docker compose exec openscientist python -m openscientist.job_manager cleanup --days 7
docker compose exec openscientist python -m openscientist.job_manager cleanup --days 7 --delete-completed
```

`make clean-jobs` wraps this with an interactive prompt for the age threshold.

For containers, `ContainerManager` provides `cleanup_job_containers(job_id)` and `cleanup_orphaned_containers(max_age_hours=24)`; agent and executor containers are force-removed when their work completes.

**Recommended practice.** Agent and executor containers are ephemeral, but an ungraceful shutdown can leave strays behind. Periodically confirm none are lingering (`docker ps --filter name=openscientist`) and monitor free disk on the host — job directories accumulate transcripts and figures, and the Postgres volume grows with them.

## Graceful Shutdown

**Mechanism.** `JobManager.shutdown()` drains in-flight job threads on SIGTERM with up to a 30-second timeout, cancelling active jobs the same way `cancel_job` does. The Compose service sets `stop_grace_period: 35s` to fit that window.

**Recommended practice.** Do not lower the grace period below the drain window, and avoid `docker kill` on the web container: a hard kill leaves jobs stuck in `RUNNING` with orphaned containers, which then needs manual cleanup.

## Logs and Diagnostics

**Mechanism.**

```bash
docker compose logs -f                          # all services
docker compose logs --tail 100 openscientist    # web app
docker compose ps                               # service status
make logs
```

Per-job iteration logs are written into the job directory as `claude_iterations.log`. Logs from the MCP tool subprocess surface through the agent's stderr handling and are included in the iteration result on failure. Setting `SQL_ECHO=true` logs all SQL queries.

Health:

```bash
curl -f http://localhost:8080/health
docker inspect openscientist-openscientist-1 --format='{{.State.Health.Status}}'
```

The `/health` endpoint is rate limited to 10 requests/minute, which matters if you point an external monitor at it.

**Recommended practice.** There is no centralized log aggregation, metrics endpoint, or alerting in this repository. For a production deployment, ship container logs off-host and alert on the health check plus the RLS/role startup errors, since those indicate an unmigrated or misconfigured database.

## Post-Deployment Verification

**Mechanism.** The Compose health check polls `/health` every 30 seconds (10s timeout, 3 retries, 40s start period). The application logs explicit RLS and role verification results at startup. Deploys are serialized per environment so a verification window is never interrupted by a concurrent release.

**Recommended practice.** After a deploy:

1. Confirm the container is healthy and the expected image tag is running (`docker compose ps`, `docker inspect`).
2. Read the startup log for the RLS and `openscientist_app` role checks; errors there mean migrations have not been applied.
3. If the release included a schema change, apply `alembic upgrade head` and re-check.
4. Exercise a login and a small job end to end.
5. Confirm no jobs are stuck in `RUNNING` from the restart (`python -m openscientist.job_manager summary`).

## Deployment Recovery and Rollback

**Mechanism.** Images are tagged per commit (`dev-`/`staging-`/`prod-<sha>`), so previous releases stay addressable in ACR. Rolling back means redeploying an earlier tag: re-run the environment's workflow from the earlier commit via `workflow_dispatch`, or repoint the `image:` line in that VM's `/opt/openscientist/docker-compose.yml` and run `docker compose pull openscientist && docker compose up -d openscientist`.

Note that the VM's Compose file is maintained on the host and is not the repository's `docker-compose.yml`, so Compose-level changes (new services, mounts, environment wiring) must be applied to each VM separately.

**Recommended practice.** Before rolling back, establish whether the release included a migration; an image rollback across an incompatible schema change will not work cleanly. Keep a record of the last known-good tag per environment so recovery does not depend on reconstructing it from workflow history.

## Background Tasks

**Mechanism.** A skill sync scheduler (`src/openscientist/skill_scheduler.py`) runs periodic background synchronization of skills at a configurable interval (default one hour). Skills can also be synced from private repositories using `GITHUB_TOKEN`.

**Recommended practice.** If skill sync is not in use for a deployment, confirm it is not producing recurring errors in the logs.
