# OpenScientist Development Guide

OpenScientist (Scientific Hypothesis Agent for Novel Discovery) is a web application that runs autonomous AI agents for scientific data analysis and hypothesis testing.

## Project Structure

```text
src/
├── openscientist/
│   ├── agent/            # AbstractAgent, AgentBackend, ClaudeCodeAgent, CodexAgent, factory
│   ├── api/              # FastAPI REST endpoints and rate limits
│   ├── auth/             # Authentication (OAuth, sessions, middleware)
│   ├── database/         # SQLAlchemy models and Alembic migrations
│   ├── job/              # Job lifecycle, scheduling, types, CLI
│   ├── job_container/    # Container-per-job isolation
│   ├── orchestrator/     # Discovery orchestration (setup, iteration, report)
│   ├── prompts/          # Agent system prompts (common.py + claude.py/codex.py)
│   ├── providers/        # LLM provider registry and implementations
│   ├── transcript/       # Typed transcript schema and per-backend translators
│   ├── webapp_components/# NiceGUI pages and components
│   ├── container_manager.py  # Executor container lifecycle
│   ├── settings.py       # Pydantic settings with TOML support
│   └── web_app.py        # Main application entry point
└── openscientist_tools/  # Standalone MCP tool server, run as `python -m openscientist_tools`
```

Agent tools live in the **separate top-level `openscientist_tools` package**, not inside `openscientist`. The agent spawns it as a stdio subprocess MCP server.

## Development Setup

```bash
# Install dependencies
uv sync

# Set up environment variables (see .env.example)
export DATABASE_URL="postgresql+asyncpg://..."
export OPENSCIENTIST_SECRET_KEY="$(openssl rand -hex 32)"
export ANTHROPIC_API_KEY="..."  # or other provider credentials

# Run database migrations
uv run alembic upgrade head

# Start development server
uv run python -m openscientist.web_app --reload
```

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=openscientist
```

### Testing Conventions

**Use real database objects, not mocks.** Tests use testcontainers to spin up a real PostgreSQL database. Always create real SQLAlchemy model instances instead of mock classes:

```python
# GOOD: Create real database records
@pytest_asyncio.fixture
async def test_job(db_session: AsyncSession, webapp_user: User) -> Job:
    job = Job(
 owner_id=webapp_user.id,
 research_question="Test research question",
 status="pending",
 )
    db_session.add(job)
    await db_session.commit()
    return job

# BAD: Don't create mock classes that bypass the database
class MockJobInfo:  # Don't do this
    job_id = "fake-id"
    status = "pending"
```

**Organize tests in submodules.** Each test file should be in its own submodule when possible, mirroring the source structure:

```text
tests/
├── webapp/
│   ├── pages/
│   │   ├── test_jobs_list.py
│   │   ├── test_job_detail.py
│   │   └── test_new_job.py
│   └── conftest.py
├── api/
│   └── test_endpoints.py
└── conftest.py
```

**NiceGUI browser simulation.** Use `user_simulation` for UI tests with `browser` as the variable name (not `user`, to avoid confusion with database User model):

```python
async with user_simulation(root=some_page) as browser:
    browser.http_client.cookies.set("session_token", session_token)
    await browser.open("/page")
    await browser.should_see("Expected content")
```

## Code Conventions

- **Python 3.12+** with type hints
- **Formatting**: `ruff format`
- **Linting**: `ruff check`
- **Type checking**: `mypy --strict`
- Use `pathlib.Path` over `os.path`
- Use f-strings for formatting
- Use pydantic for settings and validation

## UI Component Reuse

**Component reuse is critical.** All UI elements with similar functionality MUST use shared components from `src/openscientist/webapp_components/ui_components.py`.

### Error Banners

Use these instead of creating inline error displays:

```python
from openscientist.webapp_components.ui_components import (
    render_config_error_banner,  # Provider config errors
    render_alert_banner,         # Generic error/warning/info
)

# Provider configuration error (e.g., missing API key)
render_config_error_banner(provider_name, config_errors, show_back_button=False)

# Generic alert (severity: "error", "warning", "info")
render_alert_banner(
    title="Something went wrong",
    message="Detailed explanation here",
    severity="error",
    details=["Detail 1", "Detail 2"],
)
```

### Status Badges

Use `get_status_badge_props()` and `render_status_cell_slot()` for job status display.

### Adding New Components

When you need a new UI pattern used in multiple places:
1. Add a reusable function to `ui_components.py`
2. Document it in this section
3. Update existing inline code to use the component

## Key Components

### Authentication (`src/openscientist/auth/`)

- OAuth providers (Google, GitHub, ORCID, plus a mock provider for dev)
- Session management with cookies
- `@require_auth` decorator for protected pages

### Providers (`src/openscientist/providers/`)

- `check_provider_config()` - Validates LLM provider setup
- Registry (`_PROVIDER_CLASS_PATHS`): `anthropic`, `cborg`, `vertex`, `bedrock`, `foundry` (Claude Code backend); `openai`, `azure-openai`, `ollama` (Codex backend)

### Agents (`src/openscientist/agent/`)

- `AbstractAgent` is the backend-agnostic interface; `AgentBackend` identifies the runtime
- `ClaudeCodeAgent` and `CodexAgent` are the two implementations
- `factory.py` derives the backend from the configured provider — there is no separate backend setting

### Web App (`src/openscientist/webapp_components/`)

- NiceGUI-based UI
- Pages in `pages/` subdirectory
- Shared components in `utils/`

## Development Tools

Helper scripts in `tools/`:

| Script | Purpose |
|---|---|
| `check_coverage_delta.py` | Compares coverage against the `main` baseline; used by the CI `coverage-delta` job |
| `check_no_direct_push_to_main.sh` | Pre-push hook blocking direct pushes to `main` |
| `migrate_legacy_transcripts.py` | One-off migration of pre-typed-format job transcripts |
| `codex_test_mcp_server.py` | Local MCP fixture for Codex backend development |
| `safe_archive.sh` | Archive helper |

## Environment Variables

| Variable               | Required | Description                         |
| ---------------------- | -------- | ----------------------------------- |
| `DATABASE_URL`         | Yes      | PostgreSQL connection string        |
| `OPENSCIENTIST_SECRET_KEY`    | Yes      | Master secret (derives all auth keys)|
| `OPENSCIENTIST_PROVIDER` | Yes    | Provider name (anthropic, cborg, vertex, bedrock, foundry, openai, azure-openai, ollama, vllm, llamacpp). There is no default: an unset value raises at startup. The previous name `CLAUDE_PROVIDER` is no longer accepted and raises at startup if set. |
| `OPENSCIENTIST_MODEL`  | No       | Model id for the selected provider. The previous name `ANTHROPIC_MODEL` is no longer accepted and raises at startup if set. |
| `ANTHROPIC_API_KEY`    | Depends  | Required if using Anthropic         |
| `ADMIN_DATABASE_URL`   | Unless `OPENSCIENTIST_DEV_MODE=true` | Connects as `openscientist_admin` (BYPASSRLS) for admin/background operations. Startup fails if unset while dev mode is off; the gate is `OPENSCIENTIST_DEV_MODE`, not `OPENSCIENTIST_ENVIRONMENT`. |
| `OPENSCIENTIST_ENVIRONMENT` | No  | `development` (default) or `production`. Production rejects `OPENSCIENTIST_DEV_MODE=true`. |
| `OPENSCIENTIST_MAX_CONCURRENT_JOBS` | No | Max concurrent jobs (default: 1)    |
| `OPENSCIENTIST_DEV_MODE`      | No       | Enable dev mode (mock OAuth, etc.)  |

This table covers the essentials only. `.env.example` is the canonical, complete reference.

## Related Documentation

- `.env.example` - Canonical reference for every environment variable
- `docs/DESIGN.md` - Architecture and design decisions
- `docs/DEPLOYMENT.md` - Deployment guide and operations
- `docs/ENVIRONMENTS.md` - Development/staging/production model and promotion path
- `docs/CICD.md` - CI gates and deployment pipelines
- `docs/QA.md` - Testing approach and coverage policy
- `docs/code-review-governance.md` - Branch strategy and review process
- `docs/MAINTENANCE.md` - Maintenance and operational procedures
- `docs/SECURITY_REVIEW.md` - Security findings and status
- `docs/DISCOVERY_AGENT_REFERENCE.md` - Discovery agent prompt reference
