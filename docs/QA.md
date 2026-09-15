# QA and Testing

The quality gates that exist in this repository, how they are enforced, and where automated validation ends and human acceptance begins.

Everything described here is implemented in `pyproject.toml`, `.github/workflows/ci.yml`, `.pre-commit-config.yaml`, `tests/`, and `tools/`. Where something is a recommendation rather than an implemented mechanism, it is labelled as such.

Related: [`CICD.md`](CICD.md) for pipeline detail, [`code-review-governance.md`](code-review-governance.md) for the review process, [`ENVIRONMENTS.md`](ENVIRONMENTS.md) for promotion.

## Two Kinds of Validation

| | Automated engineering validation | Acceptance / UAT validation |
|---|---|---|
| Question | "Does the change work and not regress anything?" | "Does the delivered system meet the agreed requirements?" |
| Executed by | pre-commit hooks and CI, on every change | People, against a running deployment |
| Evidence | CI run status, coverage artifacts | UAT records, maintained outside this repository |
| Blocking | Yes, via branch protection on the PR | Yes, for promotion decisions |

This repository implements the left-hand column. It does not contain a UAT tool, test-case management system, or defect tracker; those are external. The documentation set here is what UAT can cite as evidence of the engineering-side controls.

## Test Suite

Tests live in `tests/`, organised to mirror the source tree:

| Area | Location |
|---|---|
| Agent abstraction and backends | `tests/agent/` |
| MCP tool package | `tests/openscientist_tools/` |
| Transcript schema and translators | `tests/transcript/` |
| Web UI (NiceGUI) | `tests/webapp/` |
| Repository tooling | `tests/tools/`, `tests/test_utils/` |
| Typing contracts | `tests/test_typing/` |
| Everything else (providers, job manager, orchestrator, auth, database, API) | `tests/` root |

### pytest configuration

From `[tool.pytest.ini_options]` in `pyproject.toml`:

- `testpaths = ["tests"]`, `pythonpath = ["tests"]`
- `python_files = "test_*.py"`, `python_functions = "test_*"`
- `asyncio_mode = "auto"` with function-scoped event loops
- One registered marker: `integration` — "marks tests as integration tests (require Docker and executor image)"

### Test levels

**Unit tests** dominate: provider configuration and environment handling, settings validation, pricing math, transcript translation, prompt construction, UI component helpers, and agent routing.

**Integration tests** exercise real dependencies rather than mocks:

- *Database-backed.* `tests/conftest.py` starts a real PostgreSQL instance via `testcontainers` (`PostgresContainer`) unless `TEST_DATABASE_URL` is supplied, in which case the provided database is used. In CI a `postgres:18` service container is supplied this way. Row-Level Security is exercised directly (`tests/test_rls.py`, `tests/test_rls_production_paths.py`), as are Alembic migrations (`tests/test_alembic_migrations.py`).
- *Docker-backed.* Tests marked `integration` require Docker and the executor image — for example container management, code execution, and `tests/test_docker_socket_proxy_integration.py`.
- *Configuration contract.* `tests/test_docker_compose_security.py` asserts security properties of the Compose configuration, so the socket-proxy boundary is regression-tested rather than only documented.

The project convention is to prefer real database objects over mock classes; see the testing conventions in [`../CLAUDE.md`](../CLAUDE.md).

**Regression protection** is the accumulated suite plus the coverage-delta gate: a change that removes or bypasses covered behaviour has to either keep the tests passing or visibly drop coverage.

### Running tests

```bash
uv run pytest                                   # full suite
uv run pytest --cov=src/openscientist --cov-report=term-missing
uv run pytest tests/webapp/                     # one area
uv run pytest -m integration                    # Docker-dependent tests only
uv run pytest -m "not integration"              # skip Docker-dependent tests
```

A configured `.env` (or exported `DATABASE_URL`) is required; see [`../CONTRIBUTING.md`](../CONTRIBUTING.md).

## Static Analysis and Types

| Gate | Command | Enforced by |
|---|---|---|
| Lint | `uv run ruff check src/ tests/` | pre-commit, CI `lint` |
| Format | `uv run ruff format --check src/ tests/` | pre-commit, CI `lint` |
| Types | `uv run mypy src/openscientist/ tests/` | pre-commit, CI `typecheck` |

Type checking covers tests as well as source.

## Coverage Policy

Two independent thresholds, both enforced automatically:

1. **Absolute floor — 75%.** `fail_under = 75` in `[tool.coverage.report]` (`pyproject.toml`). Coverage is measured over `src/openscientist`, omitting tests, caches, and virtualenvs.
2. **Relative delta — 0.5 percentage points.** The CI `coverage-delta` job compares the branch's `coverage.json` against the most recent successful `coverage-report` artifact from `main` and fails when coverage drops by more than the `--tolerance` default of `0.5` (`tools/check_coverage_delta.py`).

The delta check runs on pull requests only. If no baseline artifact is available from `main`, the download step warns and does not block.

Coverage reports (`coverage.xml`, `coverage.json`, `htmlcov/`) are uploaded as a CI artifact on every run with 30-day retention, so any PR's coverage evidence is retrievable after the fact.

## Security and Supply-Chain Checks

Part of the same PR gate rather than a separate process:

| Check | Tool | Fails on |
|---|---|---|
| Secret scanning | gitleaks (CI job and pre-commit hook) | Any detected secret in the commit range |
| Python dependency vulnerabilities | `pip-audit --local --desc` | Known advisories in installed packages |
| Dependency review | `actions/dependency-review-action@v4` | High or critical severity in PR dependency changes |
| Dockerfile lint | hadolint over all four Dockerfiles | `error`-level findings |
| Dependency currency | Dependabot, weekly | n/a — opens PRs, which run full CI |

Standing security findings and their remediation status are tracked in [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md).

## What CI Enforces on a Pull Request

Summarised from `.github/workflows/ci.yml`:

`lint` · `typecheck` · `secret-scan` · `dependency-audit` · `dependency-review` · `docker-lint` · `docker-build` (conditional) · `test` · `coverage-delta`

`test` depends on `lint` and `typecheck` passing first. Which of these are *required* to merge is governed by branch protection in GitHub repository settings, which is not represented in this repository.

## Promotion Validation

The promotion path is `development → staging → main (production)`; see [`ENVIRONMENTS.md`](ENVIRONMENTS.md).

Verified today:

- Every change reaching an environment branch has passed the same CI suite as any other PR.
- The PR template requires a Test Plan on every PR, and requires a risk assessment, rollback plan, staging validation, and QA sign-off on promotion PRs.
- Staging is a full deployment of the same image build process as production, so behaviour can be exercised before production.

Recommended, not automated: because CD does not run migrations (see [`CICD.md`](CICD.md)), a release containing a schema change should have its migration applied and verified on staging before the same release reaches production, and the startup RLS/role checks in the application log should be read as a post-deploy signal.

## Defect Handling and Revalidation

Repository-supported mechanisms:

- Defects are reported through the [bug report issue template](../.github/ISSUE_TEMPLATE/bug_report.md).
- Fixes follow the standard branch-and-PR flow on a `fix/` branch (or `hotfix/` for emergency production fixes) as described in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).
- A fix PR is subject to the same CI gates, and the coverage-delta check discourages fixing a defect without covering it.

Recommended practice: add a regression test that fails before the fix and passes after it, so the defect is revalidated automatically on every subsequent change rather than by manual retest.

This repository does not define defect severity classifications, triage SLAs, or assignment rules; those are not asserted here because there is no evidence for them.
