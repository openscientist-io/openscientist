# Contributing to OpenScientist

## Prerequisites

- Python 3.12+
- Docker
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- The system libraries WeasyPrint renders reports with, the DejaVu font the fallback PDF uses, and Graphviz for report diagrams. uv installs WeasyPrint itself but cannot install these. Without Pango the tests that render a PDF or lay out the page are skipped, and without Graphviz the diagram tests are. CI sets `OPENSCIENTIST_REQUIRE_SYSTEM_TESTS=1`, which fails those tests instead.

```bash
# Debian and Ubuntu, packages taken from Dockerfile.base
sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0 shared-mime-info fonts-dejavu-core graphviz

# macOS
brew install pango graphviz
```

## Environment Setup

⚠️ **Required before running tests or the application**

Create the env files with database configuration:

```bash
# Create the env files, generating the database passwords and the master secret
uv run openscientist setup

# Start the database, create its logins and migrate as the owner
docker compose -f docker-compose.yml up -d postgres
docker compose -f docker-compose.yml run --rm db-bootstrap
uv run alembic upgrade head
```

See [Model providers](https://docs.openscientist.io/getting-started/#model-providers) for provider-specific settings if you need to configure Claude API access.

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

## Docker

```bash
# Start the stack (builds the images first)
uv run openscientist

# Stop the stack
uv run openscientist stop

# View logs
docker compose -f docker-compose.yml logs -f
```

The app runs at <http://localhost:8080>.

## Docker (Production / Deploy)

Deploying means running the CLI on the server. Clone the repository there, and the first run creates the env files and starts the stack.

```bash
uv run openscientist
```

## Code Quality

All PRs must pass:

```bash
uv run ruff check src/ tests/   # lint
uv run mypy src/openscientist/ tests/  # types
uv run pytest                   # tests (60% coverage minimum)
```

## Pull Requests

The following are normal good sense rules that generally apply to GitHub pull requests.

Reviewing a pull request costs maintainers time. PRs deemed to be of low quality will be closed.

### LLMs

You may write code with an LLM, which is a tool much like a compiler, as long as you understand and review what it produces. If your pull request adds a chair, you should be able to say what it is made of, how many legs it has and whether it is glued or nailed. Agents MAY NOT open PRs, and you must write the description and every reply to reviewers yourself. If you can't review code in the languages this repository uses, open an issue instead and write it yourself without an LLM.

### Scope

Each pull request should be a single focused change, so two features go into two pull requests, with the second stacked on the first when it depends on it. A pull request that does several things at once, or carries unrelated refactors and formatting changes, will be closed. Before you start, search the open issues and pull requests for the same work, and if the need for a feature or the approach you have in mind is uncertain, open an issue to discuss it first. If you already have pull requests waiting on review feedback, address that feedback before opening new ones.

### Comments and Docstrings

Comments and docstrings should explain only what the code does not make evident. They should never describe the pull request or restate what the code plainly does, because walls of text are hard for reviewers to read and bloat an LLM's context for no benefit.

### Tests

Tests should exercise the real code and cover its actual corner cases. Tests that mock or reimplement the code under test will not be accepted, and every bug fix needs a regression test that fails without the fix. The [testing conventions](AGENTS.md#testing-conventions) describe how tests are organized in this repository.

### User Interface

Pull requests that change the user interface should include screenshots of the changed screens.
