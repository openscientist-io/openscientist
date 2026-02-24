# Contributing to SHANDY

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
# DATABASE_URL=postgresql+asyncpg://shandy:shandy_dev_password@localhost:5434/shandy

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
uv run pytest --cov=src/shandy --cov-report=term-missing

# Run webapp tests
uv run pytest tests/webapp/

# Type checking
uv run mypy src/shandy/ tests/

# Lint and format
uv run ruff check src/ tests/
uv run ruff format src/ tests/
```

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

## Code Quality

All PRs must pass:

```bash
uv run ruff check src/ tests/   # lint
uv run mypy src/shandy/ tests/  # types
uv run pytest                   # tests (60% coverage minimum)
```
