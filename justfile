# Default: show available recipes
default:
    @just --list

# Run the full test suite
test:
    uv run pytest

# Run linter
lint:
    uv run ruff check src/ tests/

# Check formatting (fails if not formatted)
format-check:
    uv run ruff format --check src/ tests/

# Auto-format code
format:
    uv run ruff format src/ tests/

# Run type checker
typecheck:
    uv run mypy src/

# Run all CI checks (lint and format first since they're fast)
ci: lint format-check typecheck test
