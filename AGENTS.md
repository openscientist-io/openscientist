# OpenScientist Development Guide

OpenScientist (Scientific Hypothesis Agent for Novel Discovery) is a web application that runs autonomous AI agents for scientific data analysis and hypothesis testing.

## Rules for Agents

Follow the pull request policy in [`CONTRIBUTING.md`](CONTRIBUTING.md#pull-requests). In particular:

- Never push, open pull requests or issues, or post comments, because the person you work for reviews and publishes everything.
- Never write pull request descriptions, issue bodies or replies to comments, and ask the person to write them instead.
- Keep each branch to one feature or fix, and put dependent work on its own stacked branch.
- Search the open issues and pull requests for the same work before starting.
- Leave unrelated refactors and formatting changes out of the branch.
- Write comments and docstrings only for what the code does not make evident.
- Test the real code and never a mock of it, and for a bug fix, watch the regression test fail before writing the fix.
- Remind the person to attach screenshots when the change touches the user interface.
- If the person cannot review the change, suggest that they open an issue written in their own words.
- If a request breaks one of these rules, stop and name the rule.

## Project Structure

```text
src/
├── openscientist/
│   ├── agent/            # AbstractAgent and the Claude Code, Codex and omp harnesses
│   ├── api/              # FastAPI REST endpoints
│   ├── auth/             # Authentication (OAuth, sessions, middleware)
│   ├── database/         # SQLAlchemy models and migrations
│   ├── job/              # Job lifecycle, scheduling, and types
│   ├── job_container/    # Container-per-job isolation
│   ├── orchestrator/     # Discovery orchestration (setup, iteration, report)
│   ├── providers/        # LLM providers (Anthropic, Vertex, OpenAI, Ollama, etc.)
│   ├── prompts/          # Agent system prompts
│   ├── pubmed_mirror/    # Optional local PubMed corpus for air-gapped jobs
│   ├── report/           # Report rendering to HTML and PDF
│   ├── transcript/       # One transcript schema for every harness
│   ├── webapp_components/# NiceGUI pages and components
│   ├── job_manager.py    # Starts, tracks and cancels jobs
│   ├── knowledge_state.py# A job's hypotheses, findings and literature
│   ├── llm_proxy.py      # Key-replacement proxy between job containers and the provider
│   ├── exec_broker.py    # Runs a job container's code without the Docker socket
│   ├── settings.py       # Pydantic settings
│   └── web_app.py        # Main application entry point
├── openscientist_tools/  # MCP server with the agent's tools, run inside the job container
└── openscientist_executor/ # Runs model-written code inside the executor container
```

## Development Setup

```bash
# Install dependencies
uv sync

# Set up environment variables (see .env.example, .env.db.example, .env.owner.example)
export DATABASE_URL="postgresql+asyncpg://openscientist_web:..."
export OPENSCIENTIST_SECRET_KEY="$(openssl rand -hex 32)"
export ANTHROPIC_API_KEY="..."  # or other provider credentials

# Create the database logins, then migrate as openscientist_owner (OWNER_DATABASE_URL)
docker compose run --rm db-bootstrap
uv run alembic upgrade head

# Start development server (OPENSCIENTIST_DEV_MODE=true enables auto-reload)
uv run python -m openscientist.web_app
```

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/openscientist
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

**Test layout.** Most tests sit flat in `tests/` as `test_<module>.py`. Subpackages such as `agent/`, `openscientist_tools/`, `transcript/` and `webapp/` group related tests, with shared fixtures in the `conftest.py` files of `tests/`, `tests/openscientist_tools/` and `tests/webapp/`.

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
- **Type checking**: `uv run mypy src/openscientist/ tests/`, as CI runs it
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

Use `render_status_cell_slot()` for job status display in tables and `STATUS_COLORS` elsewhere.

### Adding New Components

When you need a new UI pattern used in multiple places:
1. Add a reusable function to `ui_components.py`
2. Document it in this section
3. Update existing inline code to use the component

## Key Components

### Authentication (`src/openscientist/auth/`)

- OAuth providers (Google, GitHub, mock for dev)
- Session management with cookies
- `@require_auth` decorator for protected pages

### Providers (`src/openscientist/providers/`)

- `check_provider_config()` - Validates LLM provider setup
- Supports Anthropic, CBORG, Vertex AI, AWS Bedrock, Azure AI Foundry, OpenAI, Azure OpenAI, Ollama, vLLM and llama.cpp

### Web App (`src/openscientist/webapp_components/`)

- NiceGUI-based UI
- Pages in `pages/` subdirectory
- Shared UI components in `ui_components.py`, helpers in `utils/`

## Environment Variables

| Variable               | Required | Description                         |
| ---------------------- | -------- | ----------------------------------- |
| `DATABASE_URL`         | Yes      | PostgreSQL connection string        |
| `ADMIN_DATABASE_URL`   | Yes      | Connection string of the `openscientist_admin` login |
| `OPENSCIENTIST_SECRET_KEY`    | Yes      | Master secret (derives all auth keys)|
| `OPENSCIENTIST_PROVIDER` | Yes    | Provider name (anthropic, cborg, vertex, bedrock, foundry, openai, azure-openai, ollama, vllm, llamacpp). There is no default: an unset value raises at startup. |
| `OPENSCIENTIST_MODEL`  | No       | Model id for the selected provider. |
| `ANTHROPIC_API_KEY`    | Depends  | Required if using Anthropic         |
| `OPENSCIENTIST_MAX_CONCURRENT_JOBS` | No | Max concurrent jobs (default: 1)    |
| `OPENSCIENTIST_DEV_MODE`      | No       | Enable dev mode (mock OAuth, etc.)  |

## Related Documentation

- `docs/DESIGN.md` - Architecture and design decisions
- `docs/DEPLOYMENT.md` - Production deployment guide
