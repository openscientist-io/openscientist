# OpenScientist: Scientific Hypothesis Agent for Novel Discovery

An autonomous AI scientist that generates and tests hypotheses from scientific data.

**Live instance: [openscientist.io](https://openscientist.io)**

## Overview

OpenScientist is a domain-agnostic autonomous discovery agent that:

- Accepts data files and a research question
- Runs for N iterations autonomously
- Generates hypotheses, tests them, searches literature
- Produces a final report with findings and mechanistic insights

## Features

### Core Capabilities

- **Autonomous Discovery**: Runs iterative hypothesis-testing loop using an agentic coding assistant
- **Domain-Agnostic**: Works with genomics, transcriptomics, proteomics, metabolomics, and other scientific data
- **Literature-Grounded**: Searches PubMed for mechanistic insights
- **Multi-Provider Support**: Eight model providers across two agent backends (see [Model Providers](#model-providers))
- **Cost Tracking**: Project-level budget monitoring, with provider spend APIs and per-job token cost estimation
- **Sandboxed Execution**: Python code runs in an isolated, read-only executor container

### Skills System

- **Workflow Skills**: Hypothesis generation, result interpretation, prioritization, stopping criteria
- **Domain Skills**: Metabolomics, genomics/transcriptomics, structural biology, data science/statistics

Built-in skills live in `skills/`. Each agent backend materialises the enabled skills into the job workspace in its own layout.

### Architecture

- **Agent abstraction**: `AbstractAgent` with two backends — `ClaudeCodeAgent` (Claude Code SDK) and `CodexAgent` (Codex CLI). The backend is derived from the configured provider; there is no separate switch.
- **MCP Tools**: The `openscientist_tools` package (`src/openscientist_tools/`) runs as a stdio subprocess MCP server spawned by the agent.
  - `execute_code`: Run Python analysis
  - `search_pubmed`: Search literature
  - `read_document`: Extract text from PDF, Word, and Excel files
  - `update_knowledge_state`, `add_hypothesis`, `update_hypothesis`: Record findings and hypotheses
  - `save_iteration_summary`, `set_status`, `set_job_title`, `set_consensus_answer`: Job metadata
  - `run_phenix_tool`, `compare_structures`, `parse_alphafold_confidence` (registered only when Phenix is available)
- **Container-per-job**: Each job runs in an ephemeral agent container; each code execution runs in a further read-only executor container. No application container mounts the host Docker socket — access goes through a restricted `docker-socket-proxy`.
- **PostgreSQL**: System of record for users, jobs, findings, hypotheses, and costs, with Row-Level Security and Alembic-managed schema.
- **Web Interface**: NiceGUI UI plus a FastAPI REST API, with OAuth authentication (Google, GitHub, ORCID).

See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture.

### Structural Biology Support (Optional)

OpenScientist supports **Phenix integration** for protein structure analysis:

- Structure comparison and superposition
- Validation metrics (clash score, backbone geometry)
- AlphaFold confidence analysis

Phenix is not bundled. Install it on the host and point `PHENIX_HOST_PATH` at the installation; Docker Compose mounts it read-only into the container at `/opt/phenix`. See the Phenix variables in [.env.example](.env.example). The Phenix tools register only when the installation is detected.

## Quick Start

### Prerequisites

- Python 3.12+
- Docker and Docker Compose v2
- `uv` package manager
- Credentials for one supported model provider — see [Model Providers](#model-providers)

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd openscientist

# Create .env file (copy from example and configure)
cp .env.example .env
# Edit .env: set OPENSCIENTIST_SECRET_KEY, OPENSCIENTIST_PROVIDER, and provider credentials
# Also uncomment and set POSTGRES_PASSWORD — the initial migration requires it (see note below)

# Build and start
make build
make start

# Apply database migrations (not applied automatically at startup)
docker compose exec openscientist alembic upgrade head
```

> **`POSTGRES_PASSWORD` is required for the first migration.** The initial migration creates the `openscientist_admin` database role and fails with `POSTGRES_PASSWORD must be set before running migrations` if the variable is missing from the app container's environment. It is commented out in `.env.example` and reaches the container only via `.env`, so a first run with an unmodified `.env` will fail at this step. Later migrations do not need it.

To skip building images locally, use the prebuilt images described in [docs/ACR_IMAGES.md](docs/ACR_IMAGES.md).

### Access the UI

Open your browser to `http://localhost:8080`

## Usage

1. Upload your data files (optional - supports CSV, TSV, Excel, Parquet, JSON, PDB, mmCIF, FASTA, images, and many other file types)
2. Enter your research question
3. Set maximum iterations (e.g., 10)
4. Click "Start Discovery"
5. Monitor progress and view results

## Project Structure

```text
openscientist/
├── src/
│   ├── openscientist/          # Core Python package
│   │   ├── agent/              # AbstractAgent, AgentBackend, ClaudeCodeAgent, CodexAgent, factory
│   │   ├── api/                # FastAPI REST endpoints and rate limits
│   │   ├── auth/               # OAuth providers (Google, GitHub, ORCID, mock), sessions, middleware
│   │   ├── database/           # SQLAlchemy models and Alembic migrations
│   │   ├── job/                # Job lifecycle, scheduling, types, CLI
│   │   ├── job_container/      # Container-per-job launching
│   │   ├── orchestrator/       # Discovery orchestration (setup, iteration, report)
│   │   ├── prompts/            # System prompts (common + claude/codex variants)
│   │   ├── providers/          # Model provider registry and implementations
│   │   ├── transcript/         # Typed transcript schema and per-backend translators
│   │   ├── webapp_components/  # NiceGUI pages and shared components
│   │   ├── container_manager.py# Executor container lifecycle
│   │   ├── settings.py         # Pydantic settings
│   │   └── web_app.py          # Application entry point
│   └── openscientist_tools/    # Standalone MCP tool server (run as a subprocess)
├── docker/                     # Agent entrypoint, Postgres init (roles/RLS)
├── skills/                     # Built-in workflow and domain skills
├── tools/                      # Repository helper scripts
├── tests/                      # Test suite
├── docs/                       # Documentation (see index below)
├── jobs/                       # Job results (created at runtime)
├── Dockerfile*                 # Base, app, agent, and executor images
├── docker-compose.yml          # Container orchestration
└── Makefile                    # Build and deployment commands
```

## Configuration

[.env.example](.env.example) is the canonical reference for every supported environment variable. The sections below cover the most common setup; consult `.env.example` for the complete set.

### Required Settings

| Variable | Notes |
|---|---|
| `OPENSCIENTIST_SECRET_KEY` | Master secret; all auth keys are derived from it. Generate with `openssl rand -hex 32`. |
| `DATABASE_URL` | PostgreSQL DSN. Set automatically under Docker Compose from the `POSTGRES_*` values. |
| `OPENSCIENTIST_PROVIDER` | Provider id. Required — there is no default, and an unset value raises at startup. |
| Provider credentials | Depends on the provider selected. |

### Model Providers

Set `OPENSCIENTIST_PROVIDER` to one of the eight registered providers. The agent backend follows from that choice — you do not select it separately.

| `OPENSCIENTIST_PROVIDER` | Provider | Agent backend |
|---|---|---|
| `anthropic` | Anthropic API | Claude Code |
| `cborg` | CBORG (Lawrence Berkeley National Lab) | Claude Code |
| `vertex` | Google Vertex AI | Claude Code |
| `bedrock` | AWS Bedrock | Claude Code |
| `foundry` | Azure AI Foundry | Claude Code |
| `openai` | OpenAI | Codex |
| `azure-openai` | Azure OpenAI | Codex |
| `ollama` | Ollama (local models) | Codex |

Optionally set `OPENSCIENTIST_MODEL` to a model id valid for the selected provider.

#### Option 1: Anthropic

```bash
OPENSCIENTIST_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

#### Option 2: CBORG (Lawrence Berkeley National Lab)

```bash
# Provider selection
OPENSCIENTIST_PROVIDER=cborg

# CBORG credentials
ANTHROPIC_AUTH_TOKEN=your-cborg-token
ANTHROPIC_BASE_URL=https://api.cborg.lbl.gov
```

**Cost Tracking**: Real-time via CBORG API (`/key/info`, `/user/daily/activity`)

#### Option 3: Google Vertex AI

```bash
# Provider selection
OPENSCIENTIST_PROVIDER=vertex

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
**Setup Guide**: See the Vertex AI variables in [.env.example](.env.example), and [Claude Code on Vertex AI](https://code.claude.com/docs/en/google-vertex-ai)

#### Option 4: AWS Bedrock

```bash
# Provider selection
OPENSCIENTIST_PROVIDER=bedrock

# AWS configuration
AWS_REGION=us-east-1

# Authentication (choose one):
# Option A: Access keys
AWS_ACCESS_KEY_ID=your-access-key-id
AWS_SECRET_ACCESS_KEY=your-secret-access-key

# Option B: AWS profile
# AWS_PROFILE=your-profile-name

# Option C: Bedrock API key
# AWS_BEARER_TOKEN_BEDROCK=your-bedrock-api-key
```

**Cost Tracking**: Via AWS Cost Explorer (24-48 hour lag)
**Note**: Requires IAM permissions for `bedrock:InvokeModel` and `ce:GetCostAndUsage`

#### Option 5: Azure AI Foundry (Microsoft Foundry)

```bash
# Provider selection
OPENSCIENTIST_PROVIDER=foundry

# Azure resource configuration
ANTHROPIC_FOUNDRY_RESOURCE=your-resource-name
# Or use full URL:
# ANTHROPIC_FOUNDRY_BASE_URL=https://your-resource.services.ai.azure.com/anthropic

# Authentication (choose one):
# Option A: API key (recommended for testing)
ANTHROPIC_FOUNDRY_API_KEY=your-azure-api-key

# Option B: Azure Entra ID (automatic - no API key needed)
# Run: az login
# Or configure managed identity for production

# Model deployment names (optional - defaults shown)
ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-5
ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-6

# For cost tracking (optional):
AZURE_SUBSCRIPTION_ID=your-subscription-id
```

**Cost Tracking**: Via Azure Cost Management API (implementation in progress)
**Setup Guide**: See [Claude Code Foundry docs](https://code.claude.com/docs/en/microsoft-foundry)
**Note**: Requires Azure RBAC permissions (`Azure AI User` or `Cognitive Services User` role)

#### Options 6-8: Codex-backend providers

These providers run the Codex agent backend rather than Claude Code. The agent image ships the `codex` CLI, so no extra host installation is needed.

```bash
# OpenAI
OPENSCIENTIST_PROVIDER=openai
OPENAI_API_KEY=sk-...

# Azure OpenAI
OPENSCIENTIST_PROVIDER=azure-openai

# Ollama (local models; no API key)
OPENSCIENTIST_PROVIDER=ollama
```

See [.env.example](.env.example) for the endpoint, deployment, and model variables each of these accepts.

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
# Dev mode - enables mock OAuth login for development
OPENSCIENTIST_DEV_MODE=true
```

### Job Manager Settings

Configured through the environment, not by editing source:

```bash
# Maximum concurrent jobs (default: 1)
OPENSCIENTIST_MAX_CONCURRENT_JOBS=1

# Job data directory (default: jobs/)
OPENSCIENTIST_JOBS_DIR=jobs
```

Each concurrent job is a separate agent container, so size this against `OPENSCIENTIST_AGENT_MEMORY` and `OPENSCIENTIST_AGENT_CPU`.

### Legacy Bootstrap (Filesystem -> DB)

If you have pre-database jobs on disk, run:

```bash
docker compose exec openscientist python -m openscientist.job_manager bootstrap --jobs-dir /app/jobs --dry-run
docker compose exec openscientist python -m openscientist.job_manager bootstrap --jobs-dir /app/jobs
```

Jobs with unresolved ownership are migrated as orphaned (`owner_id=NULL`) and
can be assigned later from the admin UI.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, branch strategy, and the PR process.

## Documentation

**Architecture and configuration**

- [Design Document](docs/DESIGN.md) — architecture, agent abstraction, providers, MCP tools, containers, persistence
- [.env.example](.env.example) — canonical reference for every environment variable
- [Container Images (ACR)](docs/ACR_IMAGES.md) — using prebuilt agent/executor images
- [Discovery Agent Reference](docs/DISCOVERY_AGENT_REFERENCE.md) — the agent's system prompt, for review

**Operations**

- [Deployment Guide](docs/DEPLOYMENT.md) — configuration, Compose architecture, operations, troubleshooting
- [Environments](docs/ENVIRONMENTS.md) — development/staging/production model and promotion path
- [CI/CD](docs/CICD.md) — CI gates and the deployment pipelines
- [Maintenance](docs/MAINTENANCE.md) — dependencies, migrations, backups, rotation, rollback
- [Database Migrations](src/openscientist/database/migrations/README.md) — Alembic revision policy

**Engineering process**

- [Contributing](CONTRIBUTING.md) — setup, testing, branch and commit conventions
- [Code Review and Change Governance](docs/code-review-governance.md) — branch strategy, review expectations, CI gates
- [QA and Testing](docs/QA.md) — test suite, coverage policy, acceptance validation
- [Security Review](docs/SECURITY_REVIEW.md) — findings and remediation status

**History**

- [Agent Abstraction Migration Record](docs/AGENT_ABSTRACTION_DEPLOYMENT.md) — completed refactor, plus the legacy transcript migration step

## Author

Justin Reese <justinreese@lbl.gov>
