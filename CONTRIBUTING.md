# Contributing to SHANDY

## Prerequisites

- Python 3.10+
- Docker
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Local Development

```bash
# Install all dependencies (including dev tools)
uv sync

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=src/shandy --cov-report=term-missing

# Type checking
uv run mypy src/shandy/ tests/

# Lint and format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

## Docker (Development Mode)

Development mode mounts `src/` into the container so code changes auto-reload without rebuilding.

```bash
# First time: build the image
make build

# Start with live reload
make dev-start

# View logs
make dev-logs

# Restart / stop
make dev-restart
make dev-stop
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

## Environment

Copy `.env.example` to `.env` and configure your provider credentials. See the README for provider-specific settings.

## Code Quality

All PRs must pass:

```bash
uv run ruff check src/ tests/   # lint
uv run mypy src/shandy/ tests/  # types
uv run pytest                   # tests (60% coverage minimum)
```
