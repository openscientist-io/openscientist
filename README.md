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
- **Multiple Agent Harnesses**: Runs investigations with Claude Code, OpenAI Codex, or OMP
- **Multi-Provider Support**: Connects to Anthropic, CBORG, Vertex AI, Bedrock, Azure AI Foundry, OpenAI, Azure OpenAI, Ollama, vLLM, or llama.cpp
- **Cost Tracking**: Project-level budget monitoring with provider-specific cost APIs
- **Sandboxed Execution**: Runs model-written Python, Rust, and SPARQL in resource-limited executor containers

### Skills System

- **Workflow Skills**: Hypothesis generation, result interpretation, prioritization, stopping criteria
- **Domain Skills**: Metabolomics, genomics/transcriptomics, structural biology, data science/statistics

### Architecture

- **MCP Tools**: Provides tools via Model Context Protocol
  - `execute_code`: Run Python, Rust, or SPARQL analysis
  - `search_pubmed`: Search literature
  - `update_knowledge_state`: Record findings
  - `run_phenix_tool`, `compare_structures`, `parse_alphafold_confidence` (optional, requires Phenix)
- **Knowledge State**: PostgreSQL-backed tracking for findings, hypotheses, literature, analysis logs, and iteration summaries
- **Job Manager**: Multi-job support with queueing and lifecycle management
- **Web and REST Interfaces**: NiceGUI UI plus an authenticated FastAPI API

With container isolation enabled, each job runs in a dedicated agent container.
The agent calls the standalone `openscientist-tools` MCP server, which routes
model-written analysis through an execution broker into short-lived executor
containers. Executor containers have resource limits and no network in
air-gapped mode; the agent container can also run behind a default-deny egress
firewall.

### Structural Biology Support (Optional)

OpenScientist supports **Phenix integration** for protein structure analysis:

- Structure comparison and superposition
- Validation metrics (clash score, backbone geometry)
- AlphaFold confidence analysis

See the Phenix section in [.env.example](.env.example) for installation and
configuration notes.

## Quick Start

### Prerequisites

- Python 3.12+
- Docker (for containerized deployment)
- `uv` package manager
- Credentials for one of the supported model providers (self-hosted providers
  can run locally, and may not require an API key)

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd openscientist

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
openscientist/
├── src/
│   ├── openscientist/             # Web app, API, orchestration, and persistence
│   │   ├── agent/                 # Claude Code, Codex, and OMP harnesses
│   │   ├── api/                   # Authenticated REST endpoints
│   │   ├── database/              # PostgreSQL models, RLS, and migrations
│   │   ├── job_container/         # Per-job container lifecycle and egress policy
│   │   ├── orchestrator/          # Iterative discovery and report generation
│   │   ├── providers/             # LLM provider and cost integrations
│   │   ├── report/                # Markdown, HTML, PDF, and figure rendering
│   │   └── transcript/            # Backend-neutral transcript schema
│   ├── openscientist_tools/       # Standalone scientific MCP server
│   └── openscientist_executor/    # Isolated code-execution entry point
├── skills/                        # Built-in workflow and domain skills
├── tests/                         # Unit and integration tests
├── Dockerfile.agent               # Per-job agent image
├── Dockerfile.executor            # Analysis executor image
└── docker-compose.yml             # Web app and PostgreSQL services
```

## Configuration

### Model Providers

`OPENSCIENTIST_PROVIDER` selects model routing and authentication.
`OPENSCIENTIST_HARNESS` independently selects the coding-agent runtime; its
default value, `auto`, derives a compatible harness from the provider:

| Automatic harness | Provider IDs |
|-------------------|--------------|
| **Claude Code** | `anthropic`, `cborg`, `vertex`, `bedrock`, `foundry` |
| **Codex** | `openai`, `azure-openai`, `ollama` |
| **OMP** | `vllm`, `llamacpp` |

Set `OPENSCIENTIST_HARNESS=omp` to use OMP with any registered provider.
Explicit `claude_code` and `codex` selections require a compatible provider
family.

Choose a provider and copy its credential settings from
[.env.example](.env.example):

```bash
OPENSCIENTIST_PROVIDER=anthropic
# Add the credentials for the selected provider.
```

`OPENSCIENTIST_MODEL` optionally overrides the provider's default model. Cost
tracking and authentication capabilities vary by provider; `.env.example`
documents the corresponding settings.

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

- `OPENSCIENTIST_MAX_CONCURRENT_JOBS`: Maximum concurrent jobs (default: `1`)
- `OPENSCIENTIST_JOBS_DIR`: Directory for job artifacts (default: `jobs/`)

### Legacy Bootstrap (Filesystem -> DB)

If you have pre-database jobs on disk, run:

```bash
docker compose exec openscientist python -m openscientist.job_manager bootstrap --jobs-dir /app/jobs --dry-run
docker compose exec openscientist python -m openscientist.job_manager bootstrap --jobs-dir /app/jobs
```

Jobs with unresolved ownership are migrated as orphaned (`owner_id=NULL`) and
can be assigned later from the admin UI.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and deployment.

## Documentation

- [Design Document](docs/DESIGN.md)
- [Deployment Guide](docs/DEPLOYMENT.md)
- [Security Review](docs/SECURITY_REVIEW.md)
- [Environment Configuration](.env.example)

## Author

Justin Reese <justinreese@lbl.gov>
