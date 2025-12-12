# SHANDY

**Scientific Hypothesis Agent for Novel Discovery**

An autonomous AI scientist that generates and tests hypotheses from scientific data.

## What is SHANDY?

SHANDY is an autonomous discovery agent that helps scientists investigate research questions. You provide a question and optionally upload data files, and SHANDY iteratively:

1. **Explores** your data and searches scientific literature
2. **Generates** testable hypotheses based on patterns and prior knowledge
3. **Tests** hypotheses with statistical analysis and visualization
4. **Records** findings with evidence to a knowledge graph
5. **Synthesizes** results into a final report with mechanistic insights

SHANDY runs autonomously for a configurable number of iterations, or you can use **Coinvestigate Mode** to provide guidance between iterations.

## Features

### Investigation Modes

- **Autonomous Mode**: SHANDY runs independently for N iterations without intervention
- **Coinvestigate Mode**: After each iteration, SHANDY pauses for your feedback. You can guide the investigation, suggest new directions, or let it auto-continue after 15 minutes.

### Core Capabilities

- **Domain-Agnostic**: Works with metabolomics, genomics, transcriptomics, proteomics, and other scientific data
- **Literature-Grounded**: Searches PubMed to inform hypothesis generation and interpret findings
- **Multi-Provider Support**: Works with CBORG, Google Vertex AI, or AWS Bedrock
- **Cost Tracking**: Real-time budget monitoring with provider-specific APIs
- **Sandboxed Execution**: Safe Python code execution for data analysis

### Supported Data Formats

- **Tabular**: CSV, TSV, Excel (.xlsx), Parquet, JSON
- **Structures**: PDB, mmCIF (with optional Phenix integration)
- **Sequences**: FASTA
- **Images**: PNG, JPG
- **And many other file types** - SHANDY can work with most scientific data formats
- **No data required**: Literature-only investigations are also supported

### Web Interface

- **Research Log**: Chronological view of the investigation with iteration summaries
- **Progressive Disclosure**: See high-level summaries first, drill into code/plots/literature on demand
- **Visual Badges**: Quick indicators for analyses run, papers searched, findings recorded
- **Live Progress**: Auto-updating status with countdown timer for coinvestigate mode
- **Downloadable Reports**: Export final report as Markdown or PDF

## Quick Start

### Prerequisites

- Docker (recommended) or Python 3.10+
- API access via one of:
  - **CBORG**: Token from [cborg.lbl.gov](https://cborg.lbl.gov)
  - **Vertex AI**: GCP project with Vertex AI enabled
  - **AWS Bedrock**: *(Coming soon)*

### Installation

```bash
# Clone the repository
git clone https://github.com/justaddcoffee/shandy.git
cd shandy

# Copy and configure environment
cp .env.example .env
# Edit .env with your API credentials (see Configuration section)

# Start with Docker
make start
```

Access the web UI at `http://localhost:8080`

### Running Your First Investigation

1. Navigate to `http://localhost:8080` and log in
2. Click **New Job**
3. Enter your research question (be specific!)
4. Optionally upload data files
5. Set max iterations (10 is a good starting point)
6. Choose **Coinvestigate Mode** if you want to provide feedback between iterations
7. Click **Start Discovery**

## Configuration

### Provider Setup

Configure one provider in your `.env` file:

#### CBORG (Lawrence Berkeley National Lab)

```bash
CLAUDE_PROVIDER=cborg
ANTHROPIC_AUTH_TOKEN=your-cborg-token
ANTHROPIC_BASE_URL=https://api.cborg.lbl.gov
```

#### Google Vertex AI

```bash
CLAUDE_PROVIDER=vertex
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
CLOUD_ML_REGION=us-east5
CLAUDE_CODE_USE_VERTEX=1
```

See `docs/VERTEX_SETUP.md` for detailed setup instructions.

### Budget Controls

```bash
MAX_PROJECT_SPEND_TOTAL_USD=1000   # Total budget
MAX_PROJECT_SPEND_24H_USD=50       # Daily limit
```

### Authentication

```bash
# Generate password hash: python -c "import bcrypt; print(bcrypt.hashpw(b'yourpassword', bcrypt.gensalt()).decode())"
APP_PASSWORD_HASH=$2b$12$...
DISABLE_AUTH=false  # Set to true for local development
```

## Project Structure

```
shandy/
├── src/shandy/
│   ├── orchestrator.py      # Discovery loop orchestration
│   ├── job_manager.py       # Job lifecycle and queueing
│   ├── web_app.py           # NiceGUI web interface
│   ├── knowledge_graph.py   # State tracking (hypotheses, findings, literature)
│   ├── code_executor.py     # Sandboxed Python execution
│   ├── literature.py        # PubMed search integration
│   ├── providers/           # Model provider integrations
│   └── mcp_server/          # MCP tools for Claude
├── .claude/
│   ├── skills/              # Discovery workflow skills
│   └── CLAUDE.md            # Agent system prompt
├── jobs/                    # Job data (created at runtime)
├── notes/                   # Design documents
├── Dockerfile
├── docker-compose.yml
└── Makefile
```

## Development

### Local Development

```bash
# Install with uv
uv pip install -e .

# Run web app
python -m shandy.web_app

# CLI tools
python -m shandy.job_manager list
python -m shandy.job_manager get <job_id>
```

### Docker Development

```bash
# Build and start
make rebuild

# View logs
make logs

# Shell into container
make shell

# Stop
make stop
```

### Common Commands

| Command | Description |
|---------|-------------|
| `make start` | Start the container |
| `make stop` | Stop the container |
| `make rebuild` | Rebuild image and restart |
| `make logs` | Tail container logs |
| `make shell` | Open shell in container |
| `make deploy` | Deploy to production server |

## Deployment

```bash
# Deploy to default server (gassh)
make deploy

# Deploy to custom server
make deploy DEPLOY_HOST=myserver DEPLOY_DIR=~/shandy
```

The deploy target pulls code, preserves `.env`, and rebuilds the container.

## Troubleshooting

### Changes not appearing after code update

```bash
make rebuild
```

### Job stuck in "running" after container restart

Jobs that were running when the container stopped are automatically marked as cancelled on startup.

### CBORG HTTP 400 errors

Newer Claude CLI versions may not be compatible with CBORG. Consider switching to Vertex AI or pinning the CLI version:

```bash
npm install -g @anthropic-ai/claude-code@2.0.37
```

### Browser showing old form defaults

Hard refresh (Cmd+Shift+R / Ctrl+Shift+R) or use incognito mode.

## Architecture

SHANDY uses **Claude Code CLI** in headless mode with **MCP (Model Context Protocol)** tools:

- `execute_code`: Run Python analysis in a sandboxed environment
- `search_pubmed`: Search scientific literature
- `update_knowledge_graph`: Record findings with statistical evidence
- `save_iteration_summary`: Store plain-language summary of each iteration

The **Knowledge Graph** (JSON) tracks:
- Hypotheses (pending, supported, rejected)
- Findings with statistical evidence
- Literature references
- Analysis history
- Scientist feedback (coinvestigate mode)

## Optional: Structural Biology

SHANDY supports **Phenix integration** for protein structure analysis:
- Structure comparison and superposition
- Validation metrics
- AlphaFold confidence analysis

See `docs/PHENIX_SETUP.md` for setup.

## Author

Justin Reese <justinreese@lbl.gov>
