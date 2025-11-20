# SHANDY

**Scientific Hypothesis Agent for Novel Discovery**

An autonomous AI scientist that generates and tests hypotheses from scientific data.

## Overview

SHANDY is a domain-agnostic autonomous discovery agent that:
- Accepts data files and a research question
- Runs for N iterations autonomously
- Generates hypotheses, tests them, searches literature
- Produces a final report with findings and mechanistic insights

## Features

### Core Capabilities
- **Autonomous Discovery**: Runs iterative hypothesis-testing loop using Claude Code CLI headless mode
- **Domain-Agnostic**: Works with metabolomics, genomics, transcriptomics, and other scientific data
- **Literature-Grounded**: Searches PubMed for mechanistic insights
- **Cost-Tracked**: Integrates with CBORG API for budget monitoring
- **Sandboxed Execution**: Safe Python code execution for data analysis

### Skills System
- **Workflow Skills**: Hypothesis generation, result interpretation, prioritization, stopping criteria
- **Domain Skills**: Metabolomics, genomics/transcriptomics, data science/statistics
- **Toggleable**: Users can enable/disable skills per job

### Architecture
- **MCP Tools**: Provides tools to Claude via Model Context Protocol
  - `execute_code`: Run Python analysis
  - `search_pubmed`: Search literature
  - `update_knowledge_graph`: Record findings
- **Knowledge Graph**: JSON-based state tracking for hypotheses, findings, literature
- **Job Manager**: Multi-job support with queueing and lifecycle management
- **Web Interface**: NiceGUI-based UI for job submission and monitoring

## Quick Start

### Prerequisites

- Python 3.10+
- Docker (for containerized deployment)
- `uv` package manager
- CBORG API key (or other Anthropic API access)

### Installation

#### Option 1: Docker (Recommended)

```bash
# Clone the repository
git clone <repository-url>
cd shandy

# Create .env file with your CBORG API token
echo "ANTHROPIC_AUTH_TOKEN=your-token-here" > .env

# Start with Docker Compose
docker-compose up -d
```

#### Option 2: Local Development

```bash
# Install dependencies with uv
uv pip install -e .

# Install Claude Code CLI (if not already installed)
# See https://docs.claude.com/en/docs/claude-code

# Run the web app
python -m shandy.web_app
```

### Access the UI

Open your browser to `http://localhost:8080`

## Usage

1. Upload your data files (CSV format)
2. Enter your research question
3. Set maximum iterations (e.g., 50)
4. Choose whether to use skills (structured workflows)
5. Click "Start Analysis"
6. Monitor progress and view results

## Project Structure

```
shandy/
├── src/shandy/            # Core Python package
│   ├── orchestrator.py    # Discovery loop orchestrator
│   ├── job_manager.py     # Job lifecycle management
│   ├── web_app.py         # NiceGUI web interface
│   ├── knowledge_graph.py # JSON-based state storage
│   ├── code_executor.py   # Sandboxed Python execution
│   ├── literature.py      # PubMed search
│   ├── cost_tracker.py    # CBORG API integration
│   └── mcp_server/        # MCP tools server
├── .claude/               # Claude Code configuration
│   ├── skills/            # Discovery skills
│   │   ├── workflow/      # Generic workflow skills
│   │   └── domain/        # Domain-specific skills
│   └── CLAUDE.md          # System prompt
├── jobs/                  # Job results (created at runtime)
├── notes/                 # Design documents
├── Dockerfile             # Docker image definition
├── docker-compose.yml     # Container orchestration
└── Makefile               # Build and deployment commands
```

## Configuration

### Environment Variables

Create a `.env` file with:

```bash
# CBORG API credentials (required)
ANTHROPIC_AUTH_TOKEN=your-token-here

# Claude Code CLI path (optional, default: claude)
CLAUDE_CLI_PATH=claude
```

### Job Manager Settings

In `src/shandy/web_app.py`:
- `max_concurrent`: Maximum concurrent jobs (default: 1)
- `jobs_dir`: Directory for job data (default: `jobs/`)

## Development

### Local Development

```bash
# Install dependencies
uv pip install -e .

# Run web app with hot reload
python -m shandy.web_app

# Run job manager CLI
python -m shandy.job_manager list
python -m shandy.job_manager get <job_id>
python -m shandy.job_manager cleanup --days 7

# Run orchestrator directly
python -m shandy.orchestrator --job-dir jobs/job_xxx
```

### Docker Development

```bash
# Use development compose file (mounts source for hot reload)
docker-compose -f docker-compose.dev.yml up

# View logs
docker-compose logs -f

# Execute commands in container
docker-compose exec shandy python -m shandy.job_manager list
```

## Deployment

### Production Deployment

The Makefile includes a generic `deploy` target that works with any server:

```bash
# Deploy with default settings (gassh)
make deploy

# Deploy to custom server
make deploy DEPLOY_HOST=myserver DEPLOY_DIR=~/myapp
```

The deploy target:
1. Pulls latest code on the remote server
2. Checks that .env exists (warns if missing)
3. Builds and restarts Docker containers

**Prerequisites on production server:**
1. Clone the repository to the deployment directory
2. Create `.env` from `.env.example` and configure:
   - `ANTHROPIC_AUTH_TOKEN` - Your API token
   - `APP_PASSWORD_HASH` - BCrypt hash for login password
   - Other settings from `.env.example`
3. Stop any existing application on the target port

**Custom docker-compose files:**

For production deployments with different port mappings or configurations:

1. Create `docker-compose.production.yml` (gitignored by default)
2. Use it with: `COMPOSE_FILE=docker-compose.production.yml make start`

Server-specific deployment details are kept private (not in git).

## Troubleshooting

### Code changes not appearing in web UI

If you've updated the code but don't see changes in the web interface:

```bash
# Rebuild Docker image and restart
make rebuild
```

This rebuilds the Docker image with your latest code changes and restarts the container.

### Form fields showing old default values

If the web UI shows old default values (e.g., 50 iterations instead of 10), this is browser caching. Solutions:

- **Hard refresh**: Cmd+Shift+R (Mac) or Ctrl+Shift+R (Windows/Linux)
- **Clear cache**: Clear browser cache for localhost:8080
- **Incognito mode**: Open the UI in a private/incognito window

The actual job will still use the correct defaults from the code - this only affects what's displayed in the form.

### Missing tabs or features

If tabs like "Plots" are missing from the job detail page, the Docker container needs to be rebuilt:

```bash
make rebuild
```

## Documentation

- [Design Document](notes/design-autonomous-loop.md)
- [Skills Documentation](.claude/skills/)

## Author

Justin Reese <justinreese@lbl.gov>
