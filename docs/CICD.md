# CI/CD Process

What runs automatically on a change, and how a merged change reaches a deployed environment.

Everything here is derived from `.github/workflows/`, `.pre-commit-config.yaml`, `pyproject.toml`, and `tools/`. Related: [`ENVIRONMENTS.md`](ENVIRONMENTS.md) for the environment matrix, [`QA.md`](QA.md) for the testing approach, [`DEPLOYMENT.md`](DEPLOYMENT.md) for deployment mechanics.

## Overview

```text
   local commit                pull request / push to main            merge to env branch
        │                                  │                                   │
        ▼                                  ▼                                   ▼
  pre-commit hooks                   CI  (ci.yml)                     deploy-{dev,staging,prod}.yml
  ruff · mypy · pytest        lint · typecheck · secret-scan            az acr build  →  ACR
  gitleaks · hygiene          dependency-audit · dependency-review      az vm run-command → VM
  (pre-push: block main)      docker-lint · docker-build                docker compose pull && up -d
                              test · coverage-delta
```

## Local Pre-Commit Hooks

Managed by [pre-commit](https://pre-commit.com) (`.pre-commit-config.yaml`). Install both hook types once after cloning:

```bash
uv run pre-commit install                      # commit-stage checks
uv run pre-commit install --hook-type pre-push # blocks direct pushes to main
```

| Hook | Stage | What it does |
|---|---|---|
| `ruff` | commit | Lint with `--fix`, ignoring `E501` |
| `ruff-format` | commit | Format |
| `mypy` | commit | Type check `src/openscientist/` and `tests/` |
| `pytest` | commit | Run tests, excluding `tests/zz_e2e` and `tests/webapp/pages` |
| `gitleaks` | commit | Secret scanning |
| `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-added-large-files` (1024 KB), `check-merge-conflict` | commit | Repository hygiene |
| `no-direct-push-to-main` | pre-push | Runs `tools/check_no_direct_push_to_main.sh` |

Hooks are a fast local approximation of CI, not a replacement: the commit-stage `pytest` run deliberately skips slower suites that CI does execute.

## Continuous Integration

**Workflow:** `.github/workflows/ci.yml`
**Triggers:** every pull request, and pushes to `main`
**Default permissions:** `contents: read` (the coverage-delta job additionally requests `actions: read`)

| Job | Runs | What it enforces |
|---|---|---|
| `lint` | always | `ruff check src/ tests/` and `ruff format --check src/ tests/` |
| `typecheck` | always | `mypy src/openscientist/ tests/` |
| `secret-scan` | always | gitleaks v8.30.1 over the push/PR commit range, `--redact --exit-code 1` |
| `dependency-audit` | always | `pip-audit --local --desc` against installed packages |
| `dependency-review` | pull requests | `actions/dependency-review-action@v4` with `fail-on-severity: high` |
| `docker-lint` | always | hadolint over `Dockerfile`, `Dockerfile.agent`, `Dockerfile.base`, `Dockerfile.executor` (matrix, `failure-threshold: error`) |
| `docker-changes` | always | `dorny/paths-filter` deciding whether a Docker build is needed |
| `docker-build` | when Docker-relevant paths changed | Builds `Dockerfile.base` and `Dockerfile.executor` |
| `test` | after `lint` and `typecheck` | pytest against a real PostgreSQL service, with coverage |
| `coverage-delta` | pull requests, after `test` | Fails if coverage drops more than 0.5 points below `main` |

Python is pinned to 3.12 and dependencies are installed with `uv sync` (via `astral-sh/setup-uv@v4`, caching enabled).

### Docker build validation

`docker-build` only runs when the paths filter matches: `Dockerfile.base`, `Dockerfile.executor`, `cargo-seed/**`, `requirements-executor.txt`, `pyproject.toml`, `uv.lock`, `src/**`, or `.github/workflows/ci.yml`.

It builds the base image and pushes it to a throwaway local registry service, then builds the executor image using a named build context pointing at that registry — necessary because the isolated buildx builder cannot see an image `load`ed in an earlier step.

`Dockerfile` and `Dockerfile.agent` are **lint-only, by design**, as documented in `ci.yml`:

- `Dockerfile` copies a prebuilt codex binary from a private ACR image, which needs registry credentials this workflow should not hold (especially for fork PRs).
- `Dockerfile.agent` compiles the codex-cli fork from source — a 1,300+ crate release build that exhausts a standard two-core runner rather than completing.

Both are built for real by the deployment pipelines and by `make build`.

### Tests and coverage

The `test` job runs against a `postgres:18` service container (user/db `openscientist`, published on 5434) with `DATABASE_URL` pointed at it, so tests exercise real database behaviour rather than mocks.

```bash
uv run pytest \
  --cov=src/openscientist \
  --cov-report=term-missing \
  --cov-report=xml \
  --cov-report=json:coverage.json
```

Coverage reports (`coverage.xml`, `coverage.json`, `htmlcov/`) are uploaded as the `coverage-report` artifact on every run, retained 30 days, with `if: always()`.

A total coverage floor of **75%** is enforced by `fail_under = 75` in `pyproject.toml`.

### Coverage delta

`coverage-delta` downloads this branch's `coverage-report` artifact and the most recent successful `coverage-report` from `main`, then runs:

```bash
python3 tools/check_coverage_delta.py --current current/coverage.json --baseline baseline/coverage.json
```

The check fails when coverage falls more than **0.5 percentage points** below the baseline (`--tolerance`, default `0.5`). Downloading the baseline is `continue-on-error` with `if_no_artifact_found: warn`, so a missing baseline warns rather than blocking.

## Continuous Deployment

Three workflows, one per environment, all structurally identical.

| Workflow | Branch trigger | Target | Tag |
|---|---|---|---|
| `deploy-dev.yml` | `development` | `rg-openscientist-dev` / `vm-openscientist-dev` | `dev-<sha>` |
| `deploy-staging.yml` | `staging` | `rg-openscientist-staging` / `vm-openscientist-stg` | `staging-<sha>` |
| `deploy-prod.yml` | `main` | `rg-openscientist-prod` / `vm-openscientist-prod` | `prod-<sha>` |

All three also support `workflow_dispatch` for manual redeploys. **`main` deploys to production** — see [`ENVIRONMENTS.md`](ENVIRONMENTS.md).

### Pipeline steps

1. `actions/checkout@v4`.
2. `azure/login@v2` using `secrets.AZURE_CREDENTIALS`. The job requests `id-token: write` and `contents: read`.
3. Qualify the base image reference: the main `Dockerfile` starts `FROM openscientist-base:latest` (unqualified), which is rewritten in place to `<ACR>.azurecr.io/openscientist-base:latest`.
4. `az acr build` builds `openscientist-base:latest` and then `openscientist:<env>-<sha>` **inside ACR**, not on the runner. Server-side building is deliberate: the Rust Codex build exhausts memory on low-RAM hosts.
5. Mint a short-lived registry token with `az acr login --expose-token`.
6. `az vm run-command invoke … --command-id RunShellScript` on the target VM, which docker-logs-in with that token, rewrites the `image:` line in `/opt/openscientist/docker-compose.yml` to the new tag, then runs `docker compose pull openscientist && docker compose up -d openscientist`.

Only the `openscientist` web service is replaced; Postgres and the socket proxy keep running.

### Concurrency

Each deploy workflow sets:

```yaml
concurrency:
  group: ${{ github.workflow }}
  cancel-in-progress: false
```

`az vm run-command` allows only one execution per VM at a time, so concurrent runs would fail with a Conflict. Deploys queue instead of racing, and an in-flight deploy is never cancelled midway.

### CI is not a deployment gate

The deploy workflows trigger on `push` to their branch and do not declare a dependency on the CI workflow. CI and deployment run as independent workflows on the same push. Branch protection — requiring CI to pass before a merge can land — is the mechanism that keeps unverified code out of an environment; it is configured in GitHub repository settings and is not represented in this repository.

### Database migrations are not part of CD

> **Engineering follow-up.** None of the three deploy workflows runs `alembic upgrade head`, and the application does not migrate at startup. It only verifies at startup that RLS is enabled and forced on `jobs` and that the `openscientist_app` role lacks `SUPERUSER`/`BYPASSRLS`, logging errors otherwise.
>
> The older SSH-based `make deploy` target *does* run migrations explicitly, so the two deployment routes differ.
>
> Until reconciled, a release containing a schema change requires an operator step on the target VM:
>
> ```bash
> cd /opt/openscientist && docker compose exec openscientist alembic upgrade head
> ```
>
> This is documented as current behaviour. The workflows are not changed here.

## Rollback

Verified mechanism: every release is published under an immutable per-commit tag (`dev-`/`staging-`/`prod-<sha>`), so previous builds remain addressable in ACR. An environment is rolled back by redeploying an earlier tag — re-run the workflow from the earlier commit via `workflow_dispatch`, or repoint the `image:` line in that VM's Compose file and run `docker compose up -d openscientist`.

Recommended practice, not automated today: confirm whether the release being rolled back included a migration. Because CD does not apply migrations and images are swapped independently of schema, prefer backwards-compatible migrations so an image rollback does not require a schema downgrade. See [`MAINTENANCE.md`](MAINTENANCE.md).

## Dependency Automation

`.github/dependabot.yml` opens weekly update PRs for three ecosystems:

| Ecosystem | Directory | Open PR limit |
|---|---|---|
| `pip` | `/` | 10 |
| `docker` | `/` | 5 |
| `github-actions` | `/` | (default) |

Dependabot PRs run the full CI suite like any other pull request, including `dependency-audit` and `dependency-review`.
