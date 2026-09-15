# Contributing to OpenScientist

Thanks for your interest in contributing! This guide covers environment setup, local development, our branching/PR workflow, and how the review process works.

## Repository Context

OpenScientist is developed as a public, open-source project. The core team currently develops against a **private fork** used for active development and deployment, with a planned sync back to the public upstream project. If you're an internal contributor, work against the private fork; upstream changes go through the same review bar before being pulled into the private fork (see **Upstream Sync** below).

## Prerequisites

- Python 3.12+
- Docker
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Environment Setup

⚠️ **Required before running tests or the application**

Create a `.env` file with database configuration:

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env and uncomment the DATABASE_URL line
# For local development, use:
# DATABASE_URL=postgresql+asyncpg://openscientist:openscientist_dev_password@localhost:5434/openscientist

# Start the database with Docker
docker compose -f docker-compose.yml up -d postgres
```

See the README for provider-specific settings if you need to configure Claude API access.

## Local Development

```bash
# Install all dependencies (including dev tools)
uv sync

# Run tests (requires .env to be configured)
uv run pytest

# Run tests with coverage
uv run pytest --cov=src/openscientist --cov-report=term-missing

# Run webapp tests
uv run pytest tests/webapp/

# Type checking
uv run mypy src/openscientist/ tests/

# Lint and format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

## Branch Naming

Create feature branches from `main` using these prefixes:

| Prefix | Use |
|---|---|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `refactor/` | Code refactoring |
| `docs/` | Documentation |
| `test/` | Test additions/fixes |
| `hotfix/` | Emergency production fixes |

Use lowercase, hyphenated, descriptive names (`fix/job-status-display`, not `fix/bug`), max ~50 characters.

## Environment Branches and Promotion

Three long-lived branches map to deployed environments, each with its own workflow in `.github/workflows/`:

| Branch | Deploys to |
|---|---|
| `development` | Development environment |
| `staging` | Staging environment |
| `main` | **Production** |

Promotion runs `development → staging → main (production)`. There is no `production` branch — **merging to `main` deploys to production**, so treat `main` accordingly. Full mapping and the known caveats (staging has no dedicated `AppEnvironment` value; deploy workflows do not run migrations) are in [docs/ENVIRONMENTS.md](docs/ENVIRONMENTS.md).

## Commit Conventions

- Make atomic, logical commits while you work.
- Use conventional-style messages where practical: `fix(job): prevent crash on missing provider config`.
- PRs are **squash-merged** to `main`; the PR title becomes the final commit message, so make it descriptive.

## Opening a PR

1. Push your branch and open a PR against `main`.
2. Fill out the [PR template](./.github/PULL_REQUEST_TEMPLATE.md) — description, linked issue, test plan, self-review checklist.
3. Make sure the checks under [Code Quality](#code-quality) pass locally first.
4. Request review from at least one team member (two for promotion PRs).
5. Address feedback with new commits; don't force-push over review history mid-review.
6. Once approved and CI is green, a maintainer merges.

You cannot approve your own PR. Reviewers aim to respond within 48 hours (24 for promotion PRs). Changes to protected paths (database, security, deployment config, CI) should get sign-off from the relevant `CODEOWNERS` owner (once owners are defined — `.github/CODEOWNERS` is currently empty, so this is applied manually by reviewers).

The full review process, including promotion-PR requirements and how breaking changes are handled, is in [docs/code-review-governance.md](docs/code-review-governance.md).

## Reporting Bugs / Requesting Features

Use the issue templates: [Bug report](./.github/ISSUE_TEMPLATE/bug_report.md) or [Feature request](./.github/ISSUE_TEMPLATE/feature_request.md).

## Code Quality

All PRs must pass:

```bash
uv run ruff check src/ tests/   # lint
uv run mypy src/openscientist/ tests/  # types
uv run ruff format --check src/ tests/  # formatting
uv run pytest                   # tests (75% coverage floor, `fail_under = 75`)
```

CI (`.github/workflows/ci.yml`) also runs on every PR and blocks merging on:

- **Secret scanning** (gitleaks) over the PR's commits
- **Dependency vulnerability scanning** — `pip-audit` against installed Python packages, plus `dependency-review-action` (fails on high/critical severity)
- **Docker build validation** — hadolint on all Dockerfiles, plus full builds of `Dockerfile.base` and `Dockerfile.executor` when Docker-relevant files change. `Dockerfile` and `Dockerfile.agent` are lint-only (see comments in `ci.yml`) — the former needs a private registry credential CI shouldn't have, and the latter's dependency tree (1,300+ crates) isn't a viable full build on a standard runner
- **Coverage delta** — fails if this branch's coverage drops more than 0.5 points below `main`'s

Coverage reports (XML, JSON, HTML) are uploaded as a workflow artifact on every run, retained 30 days.

For the full CI job list see [docs/CICD.md](docs/CICD.md); for the testing approach and coverage policy see [docs/QA.md](docs/QA.md).

## Git Hooks

Hooks are managed by [pre-commit](https://pre-commit.com) (see `.pre-commit-config.yaml`). Install both hook types once after cloning:

```bash
uv run pre-commit install                     # runs lint/type/test checks on commit
uv run pre-commit install --hook-type pre-push # blocks direct pushes to main
```

## Legacy Job Migration (Filesystem -> DB)

Use this when migrating old on-disk jobs (from pre-user versions) into the
database-backed model.

### Prerequisites

- Database is running (for local dev: `docker compose -f docker-compose.yml up -d postgres`)
- Dependencies are installed (`uv sync`)
- Legacy job folders exist (default path: `jobs/`)

### Trigger Migration

```bash
# Safe preview (no database writes)
uv run python -m openscientist.job_manager bootstrap --jobs-dir jobs --dry-run

# Apply migration
uv run python -m openscientist.job_manager bootstrap --jobs-dir jobs
```

The bootstrap command currently supports:
- `--jobs-dir`
- `--dry-run`

## Docker

```bash
# Build images
make build

# Start services
make start

# View logs
make logs

# Restart / stop
make restart
make stop
```

The app runs at <http://localhost:8080>.

## Docker (Production / Deploy)

```bash
# Build and start
make rebuild

# Deploy to remote server (pulls latest code, rebuilds, restarts)
make deploy                          # default host
make deploy DEPLOY_HOST=myserver     # custom host
```

The remote server must have the repo cloned and a `.env` file configured (see `.env.example`).

## Upstream Sync (Public ↔ Private Fork)

Once upstream sync resumes:
- Changes flowing **from the private fork to public upstream** are reviewed by a Maintainer specifically for secrets, internal infrastructure references, and proprietary content before being published.
- Changes flowing **from public upstream into the private fork** are isolated on a `sync/upstream-<date>` branch and go through the standard PR/review process before touching `main` — they are never merged directly into `staging` or `main`.

## Secrets & Confidentiality

Never commit API keys, credentials, or `.env.production` / `.env.staging` values. If you accidentally commit a secret, notify a Maintainer immediately so it can be rotated — don't just delete the commit.

## Code of Conduct

Be respectful and constructive in reviews and discussion — assume good faith.

## Licensing

By contributing, you confirm the submitted code is your own work (or appropriately licensed) and may be released under the project's open-source license once synced upstream.
