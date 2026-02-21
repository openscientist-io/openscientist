# SHANDY: Scientific Hypothesis Agent for Novel Discovery

An autonomous AI scientist that generates and tests hypotheses from scientific data.

## Overview

SHANDY is a domain-agnostic autonomous discovery agent that:

- Accepts data files and a research question
- Runs for N iterations autonomously
- Generates hypotheses, tests them, searches literature
- Produces a final report with findings and mechanistic insights

## Features

### Core Capabilities

- **Autonomous Discovery**: Runs iterative hypothesis-testing loop using an agentic coding assistant
- **Domain-Agnostic**: Works with genomics, transcriptomics, proteomics, metabolomics, and other scientific data
- **Literature-Grounded**: Searches PubMed for mechanistic insights
- **Multi-Provider Support**: Works with Google Vertex AI, CBORG, AWS Bedrock, or Azure AI Foundry for model access
- **Cost Tracking**: Project-level budget monitoring with provider-specific cost APIs
- **Sandboxed Execution**: Safe Python code execution for data analysis

### Skills System

- **Workflow Skills**: Hypothesis generation, result interpretation, prioritization, stopping criteria
- **Domain Skills**: Metabolomics, genomics/transcriptomics, structural biology, data science/statistics

### Architecture

- **MCP Tools**: Provides tools via Model Context Protocol
  - `execute_code`: Run Python analysis
  - `search_pubmed`: Search literature
  - `update_knowledge_state`: Record findings
  - `run_phenix_tool`, `compare_structures`, `parse_alphafold_confidence` (optional, requires Phenix)
- **Knowledge State**: JSON-based state tracking for findings and literature
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

- Python 3.12+
- Docker (for containerized deployment)
- `uv` package manager
- One of the following for model access:
  - **CBORG**: API token from [CBORG](https://cborg.lbl.gov)
  - **Vertex AI**: GCP project with Vertex AI enabled (see `docs/VERTEX_SETUP.md`)
  - **AWS Bedrock**: AWS account with Bedrock access (see below)
  - **Azure AI Foundry**: Azure subscription with Foundry resource (see below)

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd shandy

# Create .env file (copy from example and configure)
cp .env.example .env
# Edit .env with your provider credentials

# Build and start
make build
make start
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
│   ├── agent/             # AgentExecutor protocol and SDKAgentExecutor
│   ├── job/               # Job lifecycle, scheduling, and types
│   ├── orchestrator/      # Discovery orchestration (setup, iteration, report)
│   ├── providers/         # Model provider integrations
│   │   ├── base.py        # Base provider interface
│   │   ├── messaging.py   # Consolidated send_message / client factory
│   │   ├── cborg.py       # CBORG provider
│   │   ├── vertex.py      # Google Vertex AI provider
│   │   ├── bedrock.py     # AWS Bedrock provider
│   │   └── foundry.py     # Azure AI Foundry provider
│   ├── tools/             # @tool-decorated callables for agent
│   ├── mcp_server/        # MCP tools server
│   ├── web_app.py         # NiceGUI web interface
│   ├── knowledge_state.py # JSON-based state storage
│   ├── code_executor.py   # Sandboxed Python execution
│   └── literature.py      # PubMed search
├── CLAUDE.md              # Development guide and system prompt
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

```bash
# Provider selection
CLAUDE_PROVIDER=bedrock

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

#### Option 4: Azure AI Foundry (Microsoft Foundry)

```bash
# Provider selection
CLAUDE_PROVIDER=foundry

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
SHANDY_DEV_MODE=true
```

### Job Manager Settings

In `src/shandy/web_app.py`:

- `max_concurrent`: Maximum concurrent jobs (default: 1)
- `jobs_dir`: Directory for job data (default: `jobs/`)

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and deployment.

## Documentation

- [Design Document](docs/DESIGN.md)
- [Vertex AI Setup](docs/VERTEX_SETUP.md)
- [Phenix Setup](docs/PHENIX_SETUP.md)

## Author

Justin Reese <justinreese@lbl.gov>
