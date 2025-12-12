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
- **Multi-Provider Support**: Works with CBORG, Google Vertex AI, or AWS Bedrock for model access
- **Cost Tracking**: Project-level budget monitoring with provider-specific cost APIs
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
  - `run_phenix_tool`, `compare_structures`, `parse_alphafold_confidence` (optional, requires Phenix)
- **Knowledge Graph**: JSON-based state tracking for hypotheses, findings, literature
- **Job Manager**: Multi-job support with queueing and lifecycle management
- **Web Interface**: NiceGUI-based UI for job submission and monitoring

### Structural Biology Support (Optional)

SHANDY supports **Phenix integration** for protein structure analysis:
- Structure comparison and superposition
- Validation metrics (clash score, backbone geometry)
- AlphaFold confidence analysis
- **See `docs/PHENIX_SETUP.md` for installation instructions**

## Quick Start

### Prerequisites

- Python 3.10+
- Docker (for containerized deployment)
- `uv` package manager
- One of the following for model access:
  - **CBORG**: API token from [CBORG](https://cborg.lbl.gov)
  - **Vertex AI**: GCP project with Vertex AI enabled (see `docs/VERTEX_SETUP.md`)
  - **AWS Bedrock**: *(Coming soon)*

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

1. Upload your data files (optional - supports CSV, TSV, Excel, Parquet, JSON, PDB, mmCIF, FASTA, images, and many other file types)
2. Enter your research question
3. Set maximum iterations (e.g., 10)
4. Click "Start Discovery"
5. Monitor progress and view results

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
│   ├── providers/         # Model provider integrations
│   │   ├── base.py        # Base provider interface
│   │   ├── cborg.py       # CBORG provider
│   │   ├── vertex.py      # Google Vertex AI provider
│   │   └── bedrock.py     # AWS Bedrock provider (stub)
│   └── mcp_server/        # MCP tools server
├── .claude/               # Claude Code configuration
│   ├── skills/            # Discovery skills
│   │   ├── workflow/      # Generic workflow skills
│   │   └── domain/        # Domain-specific skills
│   └── CLAUDE.md          # System prompt
├── jobs/                  # Job results (created at runtime)
├── Dockerfile             # Docker image definition
├── docker-compose.yml     # Container orchestration
└── Makefile               # Build and deployment commands
```

## Configuration

### Model Providers

SHANDY supports multiple model providers. Choose one and configure it in your `.env` file:

#### Option 1: CBORG (Lawrence Berkeley National Lab)

```bash
# Provider selection
CLAUDE_PROVIDER=cborg

# CBORG credentials
ANTHROPIC_AUTH_TOKEN=your-cborg-token
ANTHROPIC_BASE_URL=https://api.cborg.lbl.gov
```

**Cost Tracking**: Real-time via CBORG API (`/key/info`, `/user/daily/activity`)

#### Option 2: Google Vertex AI

```bash
# Provider selection
CLAUDE_PROVIDER=vertex

# Vertex AI configuration
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
CLOUD_ML_REGION=us-east5
VERTEX_REGION_CLAUDE_4_5_SONNET=us-east5
VERTEX_REGION_CLAUDE_4_5_HAIKU=us-east5

# BigQuery billing export (for cost tracking)
GCP_BILLING_ACCOUNT_ID=XXXXXX-YYYYYY-ZZZZZZ
```

**Cost Tracking**: Via GCP BigQuery billing export (1-6 hour lag)
**Setup Guide**: See `docs/VERTEX_SETUP.md` for detailed instructions

#### Option 3: AWS Bedrock

*(Coming soon)*

### Budget Controls

Set application-level budget limits (optional):

```bash
# Maximum total spend across all jobs
MAX_PROJECT_SPEND_TOTAL_USD=1000

# Maximum spend in last 24 hours
MAX_PROJECT_SPEND_24H_USD=50
```

Budget limits are checked before job creation. The web UI displays:
- Total project spend
- Recent spend (last 24h)
- Budget remaining (if provider supports it)

### Other Settings

```bash
# Claude Code CLI path (optional, default: claude)
CLAUDE_CLI_PATH=claude

# Web app authentication (optional)
APP_PASSWORD_HASH=<bcrypt-hash>
DISABLE_AUTH=false
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

### CBORG HTTP 400 errors with newer Claude versions

**Symptom**: Jobs fail with HTTP 400 errors when using CBORG provider.

**Cause**: Newer versions of the Claude CLI may send headers that CBORG doesn't recognize or support, causing the CBORG API to reject requests with HTTP 400 errors.

**Solution**: Consider using an older version of the Claude CLI if you encounter this issue:

```bash
# Check current Claude CLI version
claude --version

# If needed, install a specific older version
npm install -g @anthropic-ai/claude-code@<version>
```

**Alternative**: Switch to Vertex AI provider (see `docs/VERTEX_SETUP.md`), which may have better compatibility with newer Claude CLI versions.

## Documentation

- [Design Document](notes/design-autonomous-loop.md)
- [Skills Documentation](.claude/skills/)

## Author

Justin Reese <justinreese@lbl.gov>
