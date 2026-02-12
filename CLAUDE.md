# SHANDY Development Guide

SHANDY (Scientific Hypothesis Agent for Novel Discovery) is a web application that runs autonomous AI agents for scientific data analysis and hypothesis testing.

## Project Structure

```text
src/shandy/
├── api/              # FastAPI REST endpoints
├── auth/             # Authentication (OAuth, sessions, middleware)
├── database/         # SQLAlchemy models and migrations
├── mcp_server/       # MCP server for agent tools
├── providers/        # LLM provider abstractions (Anthropic, Vertex, etc.)
├── webapp_components/# NiceGUI pages and components
├── job_manager.py    # Job lifecycle management
├── orchestrator.py   # Agent orchestration loop
├── prompts.py        # Agent system prompts
└── web_app.py        # Main application entry point
```

## Development Setup

```bash
# Install dependencies
uv sync

# Set up environment variables (see .env.example)
export DATABASE_URL="postgresql+asyncpg://..."
export TOKEN_ENCRYPTION_KEY="..."
export AUTH_STORAGE_SECRET="..."
export ANTHROPIC_API_KEY="..."  # or other provider credentials

# Run database migrations
uv run alembic upgrade head

# Start development server
uv run python -m shandy.web_app --reload
```

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=shandy

# Run E2E tests only
uv run pytest tests/zz_e2e/ -v

# Run E2E tests with visible browser
uv run pytest tests/zz_e2e/ -v --headed
```

## Code Conventions

- **Python 3.10+** with type hints
- **Formatting**: `ruff format`
- **Linting**: `ruff check`
- **Type checking**: `mypy --strict`
- Use `pathlib.Path` over `os.path`
- Use f-strings for formatting
- Use pydantic for settings and validation

## UI Component Reuse

**Component reuse is critical.** All UI elements with similar functionality MUST use shared components from `src/shandy/webapp_components/ui_components.py`.

### Error Banners

Use these instead of creating inline error displays:

```python
from shandy.webapp_components.ui_components import (
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

### Authentication (`src/shandy/auth/`)

- OAuth providers (Google, GitHub, mock for dev)
- Session management with cookies
- `@require_auth` decorator for protected pages

### Providers (`src/shandy/providers/`)

- `check_provider_config()` - Validates LLM provider setup
- Supports: Anthropic, CBORG, Vertex AI, Bedrock

### Web App (`src/shandy/webapp_components/`)

- NiceGUI-based UI
- Pages in `pages/` subdirectory
- Shared components in `utils/`

## Development Tools

See `tools/README.md` for helper scripts.

### tile_screenshots.py

Creates tiled images from screenshots for documenting UI flows:

```bash
uv run python tools/tile_screenshots.py \
  screenshots/*.png \
  -o output.png \
  -a annotations.json \
  -c 2
```

## Environment Variables

| Variable               | Required | Description                         |
| ---------------------- | -------- | ----------------------------------- |
| `DATABASE_URL`         | Yes      | PostgreSQL connection string        |
| `TOKEN_ENCRYPTION_KEY` | Yes      | 32+ char encryption key             |
| `AUTH_STORAGE_SECRET`  | Yes      | NiceGUI storage secret              |
| `CLAUDE_PROVIDER`      | No       | Provider name (default: anthropic)  |
| `ANTHROPIC_API_KEY`    | Depends  | Required if using Anthropic         |
| `ENABLE_MOCK_AUTH`     | No       | Enable mock OAuth for development   |

## Related Documentation

- `docs/DESIGN.md` - Architecture and design decisions
- `docs/DEPLOYMENT.md` - Production deployment guide
- `docs/DISCOVERY_AGENT_REFERENCE.md` - Discovery agent prompt reference
