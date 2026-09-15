# OpenScientist Deployment Guide

How to deploy OpenScientist, from a local Docker Compose stack to the CI/CD-driven Azure environments.

- Multi-environment model (development / staging / production): [`ENVIRONMENTS.md`](ENVIRONMENTS.md)
- CI gates and the deployment pipelines themselves: [`CICD.md`](CICD.md)
- Prebuilt images from Azure Container Registry: [`ACR_IMAGES.md`](ACR_IMAGES.md)
- Full configuration reference: [`.env.example`](../.env.example)

## Prerequisites

- Docker and Docker Compose v2 (all commands below use `docker compose`, not the legacy `docker-compose`)
- An LLM provider account and credentials
- For local image builds: enough memory to compile the Codex CLI, or use the prebuilt ACR images instead

## Required Configuration

Settings are validated at startup by `src/openscientist/settings.py`. These have **no defaults** — the application fails to start without them.

| Variable | Required | Notes |
|---|---|---|
| `OPENSCIENTIST_SECRET_KEY` | Yes | Master secret. Derives `storage_secret` and `token_encryption_key` via HMAC-SHA256. Generate with `openssl rand -hex 32`. |
| `DATABASE_URL` | Yes | PostgreSQL DSN, `postgresql+asyncpg://…`. Under Compose this is set automatically from the `POSTGRES_*` values and the `postgres` service hostname. |
| `OPENSCIENTIST_PROVIDER` | Yes | One of `anthropic`, `cborg`, `vertex`, `bedrock`, `foundry`, `openai`, `azure-openai`, `ollama`. No default; an unset value raises at startup. |
| Provider credentials | Yes | Depends on the selected provider (for example `ANTHROPIC_API_KEY` for `anthropic`). See [`.env.example`](../.env.example). |
| `ADMIN_DATABASE_URL` | Unless `OPENSCIENTIST_DEV_MODE=true` | Connects as the `openscientist_admin` role (`BYPASSRLS`) for admin and background operations. Startup **fails** when this is unset and `OPENSCIENTIST_DEV_MODE` is not enabled; with dev mode on, the app logs a warning and falls back to `DATABASE_URL`. Note the gate is `OPENSCIENTIST_DEV_MODE`, not `OPENSCIENTIST_ENVIRONMENT` — `OPENSCIENTIST_ENVIRONMENT=development` alone does not enable the fallback, because `OPENSCIENTIST_DEV_MODE` defaults to `false`. Under Docker Compose this is set for you. |
| `OPENSCIENTIST_ENVIRONMENT` | Recommended | `development` or `production`. Production rejects `OPENSCIENTIST_DEV_MODE=true` at startup. |

Two legacy names are rejected rather than silently accepted: `CLAUDE_PROVIDER` (now `OPENSCIENTIST_PROVIDER`) and `ANTHROPIC_MODEL` (now `OPENSCIENTIST_MODEL`). If either is set in the environment, startup fails with an explanatory error.

Commonly tuned optional settings:

| Variable | Default | Purpose |
|---|---|---|
| `OPENSCIENTIST_MAX_CONCURRENT_JOBS` | `1` | Maximum jobs running at once. |
| `PORT` | `8080` | Web server port. |
| `OPENSCIENTIST_USE_CONTAINER_ISOLATION` | `true` (Compose) | Container-per-job isolation. |
| `OPENSCIENTIST_AGENT_IMAGE` / `OPENSCIENTIST_EXECUTOR_IMAGE` | `…:latest` | Images used for spawned containers. |
| `OPENSCIENTIST_DOCKER_CONFIG` | empty config | Registry credentials for pulling agent/executor images from a private registry. |

## Quick Start (local / single host)

### 1. Configure

```bash
cp .env.example .env
# Edit .env: set OPENSCIENTIST_SECRET_KEY, OPENSCIENTIST_PROVIDER and its credentials
```

### 2. Build images

```bash
make build
```

This builds four images: `openscientist-base`, the main `openscientist` app image, `openscientist-executor`, and `openscientist-agent`. To skip the Rust/Codex compile, use the prebuilt ACR images instead — see [`ACR_IMAGES.md`](ACR_IMAGES.md).

### 3. Start the stack

```bash
make start
```

### 4. Apply database migrations

Migrations are **not** applied automatically by the application at startup. Run them explicitly against a running stack:

```bash
docker compose exec openscientist alembic upgrade head
```

> **Fresh database: set `POSTGRES_PASSWORD` in `.env` first.**
>
> The initial migration creates the `openscientist_admin` role and reads `POSTGRES_PASSWORD` from the environment of the process running Alembic, aborting with `POSTGRES_PASSWORD must be set before running migrations` if it is absent. `POSTGRES_PASSWORD` is commented out in `.env.example`, and `docker-compose.yml` passes it to the `postgres` service but does not list it in the `openscientist` service's `environment:` block — the app container only receives it through `env_file: .env`.
>
> So on a first run with an unmodified `.env`, this command fails even though Postgres itself started fine on its default password. Uncomment and set `POSTGRES_PASSWORD` in `.env` before migrating, and use the same value the database was initialised with. Once the initial revision has been applied, later `alembic upgrade head` runs do not need it.
>
> The same prerequisite applies to `make reset-db`, which runs migrations the same way.

`make reset-db` performs the destructive equivalent for local development (drops volumes, restarts Postgres, runs `alembic upgrade head`, restarts the app).

At startup the app verifies that Row-Level Security is enabled and forced on the `jobs` table and that the `openscientist_app` role exists without `SUPERUSER`/`BYPASSRLS`. If migrations have not been applied, these checks log errors telling you to run `alembic upgrade head`.

### 5. Access the web interface

<http://localhost:8080>

## Compose Architecture

`docker-compose.yml` defines three long-running services plus one build-only service.

| Service | Role |
|---|---|
| `postgres` | PostgreSQL 18. Data in the `postgres_data` named volume. `docker/postgres/init.sql` creates the `openscientist_admin` role used for RLS bypass. Health-checked with `pg_isready`. |
| `docker-socket-proxy` | Restricted Docker API proxy. The **only** container that mounts `/var/run/docker.sock` (read-only). No published ports. |
| `openscientist` | Web app (NiceGUI + FastAPI) on port 8080, plus the JobManager. Health-checked against `/health`. |
| `openscientist-agent` | Build-only (`profiles: ["build-only"]`); never started by `docker compose up`. Agent containers are created at runtime by `JobContainerRunner`. |

### The Docker socket boundary

The web app and the per-job agent containers never see the raw host socket. They reach the Docker API over `DOCKER_HOST=tcp://docker-socket-proxy:2375`, and the proxy permits only the verbs the job lifecycle needs: container list/inspect/logs/create/start/stop/wait/remove, image inspect/pull, plus ping and version negotiation. `EXEC`, `INFO`, `NETWORKS`, `VOLUMES`, `BUILD`, `COMMIT`, `AUTH`, `SYSTEM`, `SWARM`, `SECRETS`, `CONFIGS`, `SERVICES`, `TASKS`, `NODES`, `PLUGINS`, `DISTRIBUTION`, `SESSION`, and `EVENTS` are denied.

Treat this as a required security control. If you write your own Compose file for a deployment, do not replace the proxy with a direct socket mount.

### Runtime containers

Neither of these is a Compose service; both are created on demand.

- **Agent container** (`openscientist-agent`) — one ephemeral container per job, launched by `JobContainerRunner` with `no-new-privileges:true` and memory/CPU limits (`OPENSCIENTIST_AGENT_MEMORY`, `OPENSCIENTIST_AGENT_CPU`). Runs as a non-root user.
- **Executor container** (`openscientist-executor`) — started by `ContainerManager` for each code execution, with a read-only filesystem, `no-new-privileges:true`, and memory/CPU limits (`OPENSCIENTIST_EXECUTOR_MEMORY`, `OPENSCIENTIST_EXECUTOR_CPU`).

### Mounts

The `openscientist` service mounts:

- `./jobs` → `/app/jobs` — job data
- `./data` → `/app/data` — uploaded data files
- `./src` → `/app/src` — source, for live reload during development
- `./skills` → `/app/skills` (read-only) — built-in skills
- Optional, driven by `.env`: GCP credentials (`GCP_CREDENTIALS_FILE`), codex auth (`CODEX_AUTH_FILE`), Phenix (`PHENIX_HOST_PATH` → `/opt/phenix`, read-only)
- `OPENSCIENTIST_DOCKER_CONFIG` → `/root/.docker/config.json` (read-only) — registry pull credentials, defaulting to an empty-but-valid config

`.env` itself is supplied via Compose `env_file`, not as a mount. Postgres data lives in the `postgres_data` named volume.

### Shutdown behaviour

`JobManager.shutdown()` drains in-flight job threads with up to a 30-second timeout on SIGTERM, so the service sets `stop_grace_period: 35s`. Do not lower it below the drain window, or in-flight jobs will be SIGKILLed and left as stuck `RUNNING` rows with orphaned containers.

## Deployed Environments

Three environments are deployed from GitHub Actions to Azure VMs. Branch mapping:

| Branch | Workflow | Azure resource group | VM | Image tag |
|---|---|---|---|---|
| `development` | `deploy-dev.yml` | `rg-openscientist-dev` | `vm-openscientist-dev` | `dev-<sha>` |
| `staging` | `deploy-staging.yml` | `rg-openscientist-staging` | `vm-openscientist-stg` | `staging-<sha>` |
| `main` | `deploy-prod.yml` | `rg-openscientist-prod` | `vm-openscientist-prod` | `prod-<sha>` |

**`main` is the production branch.** Merging to `main` triggers a production deployment.

Each workflow: logs into Azure, builds `openscientist-base` and the main image server-side with `az acr build`, then invokes `az vm run-command` on the target VM to docker-login to ACR, repoint the image tag in the VM's `/opt/openscientist/docker-compose.yml`, and run `docker compose pull openscientist && docker compose up -d openscientist`.

Two consequences worth knowing:

- The VM's Compose file is **not** the repository's `docker-compose.yml`. It pins a published `image:` rather than using `build:`, and is maintained on the VM.
- Deploys are serialized per workflow (`concurrency.group`, `cancel-in-progress: false`), because `az vm run-command` allows only one execution per VM at a time. Overlapping runs queue rather than race.

Full detail in [`ENVIRONMENTS.md`](ENVIRONMENTS.md) and [`CICD.md`](CICD.md).

### Migrations on deploy — known gap

> **Engineering follow-up.** The deployment workflows do **not** run `alembic upgrade head`. The application does not apply migrations at startup either. By contrast, the `make deploy` target (SSH-based, used for the older single-host flow) does run migrations as an explicit step.
>
> Until this is reconciled, a release that includes a schema change requires an operator to apply migrations on the target VM after the deploy:
>
> ```bash
> cd /opt/openscientist && docker compose exec openscientist alembic upgrade head
> ```
>
> Check the app logs for the RLS/role startup checks described above to confirm the schema is current. This is a documented gap, not a recommendation to change the workflows.

## Operations

### Managing jobs

The job CLI is `python -m openscientist.job_manager`, with subcommands `list`, `get`, `delete`, `cleanup`, `summary`, and `bootstrap`.

```bash
docker compose exec openscientist python -m openscientist.job_manager list
docker compose exec openscientist python -m openscientist.job_manager list --status running --limit 20
docker compose exec openscientist python -m openscientist.job_manager get <job_id>
docker compose exec openscientist python -m openscientist.job_manager summary
docker compose exec openscientist python -m openscientist.job_manager delete <job_id>
```

Clean up old jobs (`make clean-jobs` wraps this interactively):

```bash
docker compose exec openscientist python -m openscientist.job_manager cleanup --days 7
docker compose exec openscientist python -m openscientist.job_manager cleanup --days 7 --delete-completed
```

### Legacy job bootstrap (filesystem → database)

For deployments that still have pre-database job directories on disk:

```bash
docker compose exec openscientist python -m openscientist.job_manager bootstrap --jobs-dir /app/jobs --dry-run
docker compose exec openscientist python -m openscientist.job_manager bootstrap --jobs-dir /app/jobs
```

Jobs whose ownership cannot be resolved are migrated as orphaned (`owner_id=NULL`) and can be assigned later from the admin UI.

### Concurrency

Set `OPENSCIENTIST_MAX_CONCURRENT_JOBS` in the environment. `JobManager` reads it through settings at startup — do not edit source to change it.

```bash
OPENSCIENTIST_MAX_CONCURRENT_JOBS=3
```

Size it against `OPENSCIENTIST_AGENT_MEMORY` and `OPENSCIENTIST_AGENT_CPU`: each concurrent job is a separate agent container, and each running `execute_code` adds an executor container on top.

### Logs and status

```bash
docker compose logs -f                 # follow all services
docker compose logs --tail 100 openscientist
docker compose ps
make logs
```

Per-job iteration logs are written into the job directory as `claude_iterations.log`.

### Health check

The `openscientist` service is health-checked against `/health` every 30 seconds (10s timeout, 3 retries, 40s start period).

```bash
curl -f http://localhost:8080/health
docker inspect openscientist-openscientist-1 --format='{{.State.Health.Status}}'
```

The health endpoint is rate limited to 10 requests/minute.

### Cost and budget

Spend is surfaced in the web UI: provider spend via each provider's `get_cost_info()` where the platform offers a billing API, and per-job estimated token cost from `providers/pricing.py`. Budget limits are configured via the budget variables in [`.env.example`](../.env.example) and checked before job creation. There is no CLI budget command.

### Backup and restore

Job artifacts on disk:

```bash
tar -czf openscientist-jobs-backup-$(date +%Y%m%d).tar.gz jobs/
tar -xzf openscientist-jobs-backup-YYYYMMDD.tar.gz
```

The database holds the authoritative application state and must be backed up separately — job artifacts alone are not a complete backup. See [`MAINTENANCE.md`](MAINTENANCE.md).

## Troubleshooting

### Container won't start

```bash
docker compose logs
docker compose config          # verify interpolated configuration
```

Startup failures are most often a missing required setting; the validation error names the variable. Rebuild from scratch with:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Jobs fail immediately with a 401 on `images/create`

The agent or executor image is in a private registry and no pull credentials are available. Point `OPENSCIENTIST_DOCKER_CONFIG` at a `config.json` containing valid credentials; Compose mounts it read-only at `/root/.docker/config.json`. See [`ACR_IMAGES.md`](ACR_IMAGES.md).

### Job stuck in "running"

```bash
docker compose exec openscientist python -m openscientist.job_manager get <job_id>
cat jobs/<job_id>/claude_iterations.log
docker ps --filter name=openscientist
```

Cancelling is exposed through the web UI (`JobManager.cancel_job`). Note that a hard kill of the web container without the 35-second grace period can leave jobs in this state.

### RLS or role errors in the logs

The startup checks report that migrations have not been applied, or that `openscientist_app` has excessive privileges. Apply migrations (`alembic upgrade head`) and confirm the roles created by `docker/postgres/init.sql` and the initial migration are intact.

## Updating and Rollback

For the deployed environments, releases and rollbacks go through the pipelines described in [`CICD.md`](CICD.md); because images are tagged per commit (`dev-<sha>`, `staging-<sha>`, `prod-<sha>`), rolling back is a matter of redeploying a previously published tag. Migration handling is subject to the gap noted above and should be considered when rolling back across a schema change.

For a local or manually managed host:

```bash
git pull origin main
make rebuild
docker compose exec openscientist alembic upgrade head
```

## Uninstalling

```bash
docker compose down            # stop and remove containers
docker compose down -v         # also remove volumes (WARNING: deletes the database)
```

## Support

For issues or questions, open an issue on GitHub.
