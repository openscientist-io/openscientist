# SHANDY: Scientific Hypothesis Agent for Novel Discovery

## Design Document

**Status:** Implemented
**Last Updated:** December 2025

---

## What is SHANDY?

SHANDY is an **autonomous AI scientist** that discovers mechanistic insights from scientific data through iterative hypothesis testing. Given a research question and optional data files, SHANDY:

1. Explores the data to understand its structure and patterns
2. Searches scientific literature (PubMed) for domain knowledge
3. Generates and tests hypotheses using statistical analysis
4. Records findings with supporting evidence and visualizations
5. Produces a final report synthesizing all discoveries

SHANDY runs **autonomously for N iterations**, making its own decisions about what to investigate next. It can also operate in **coinvestigate mode**, pausing after each iteration to receive guidance from a human scientist.

---

## Core Design Philosophy

### Domain-Agnostic

SHANDY is not tied to any specific scientific domain. It works with:
- Genomics and transcriptomics
- Proteomics
- Structural biology (protein structures)
- Metabolomics data
- General tabular scientific data
- Literature-only investigations (no data files required)

Domain-specific knowledge is provided through a **skills system** rather than hard-coded logic.

### Code-Writing Agent

Rather than providing pre-defined statistical functions, SHANDY writes Python code to analyze data. This gives it:
- **Flexibility**: Can invent novel analyses on the fly
- **Transparency**: All analysis code is logged and reproducible
- **Autonomy**: Not limited to anticipated use cases

### Literature-Grounded Discovery

SHANDY proactively searches PubMed to inform hypothesis generation and interpret results. Literature provides:
- Mechanistic context for observations
- Known pathways and regulatory relationships
- Validation of unexpected findings

---

## Technical Architecture

### How It Works

SHANDY uses an **agentic coding assistant** as its reasoning engine. The current implementation uses Claude Code CLI in headless mode, though the architecture is designed to potentially support other agentic frameworks in the future.

The orchestrator spawns the agent with:
- A system prompt containing the research question and context
- An MCP server providing scientific tools (code execution, PubMed search, etc.)
- Access to domain-specific skills

```
┌─────────────────────────────────────────────────────────────┐
│  SHANDY CONTAINER                                           │
│                                                             │
│  ┌─────────────────────┐    ┌─────────────────────────┐    │
│  │   NiceGUI Web UI    │    │    Job Manager          │    │
│  │   - Submit jobs     │───▶│    - Queue jobs         │    │
│  │   - Monitor progress│    │    - Track status       │    │
│  │   - View results    │    │    - Manage lifecycle   │    │
│  └─────────────────────┘    └──────────┬──────────────┘    │
│                                        │                    │
│                                        ▼                    │
│  ┌──────────────────────────────────────────────────────┐  │
│  │   Orchestrator (per job)                              │  │
│  │   - Spawns agentic coding assistant                   │  │
│  │   - Runs N iterations of discovery                    │  │
│  │   - Saves transcripts and provenance                  │  │
│  │   - Handles coinvestigate mode feedback               │  │
│  └──────────────────────────────────────────────────────┘  │
│                         │                                   │
│           ┌─────────────┴─────────────┐                    │
│           ▼                           ▼                    │
│  ┌─────────────────┐        ┌─────────────────────┐        │
│  │  Agentic        │◀──────▶│  MCP Server         │        │
│  │  Coding         │        │  - execute_code     │        │
│  │  Assistant      │        │  - search_pubmed    │        │
│  │                 │        │  - update_knowledge │        │
│  │  Reasons about  │        │  - save_summary     │        │
│  │  what to do     │        │  - phenix_tools*    │        │
│  │  next           │        │                     │        │
│  └─────────────────┘        └─────────────────────┘        │
│                                        │                    │
│                                        ▼                    │
│  ┌──────────────────────────────────────────────────────┐  │
│  │   Knowledge State (JSON)                              │  │
│  │   - Findings with evidence                            │  │
│  │   - Literature references                             │  │
│  │   - Analysis log                                      │  │
│  │   - Iteration summaries                               │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

> **Note:** The current implementation is built around Claude Code CLI and Anthropic's Claude models. Future versions will support other agentic frameworks.

### The Discovery Loop

Each job runs through multiple iterations:

```
For each iteration (1 to N):
    1. Agent receives current knowledge state + prompt
    2. Agent decides what to investigate
    3. Agent calls tools (execute code, search literature, etc.)
    4. MCP server executes tools, returns results
    5. Agent interprets results, records findings
    6. Agent saves iteration summary
    7. Orchestrator increments iteration, saves transcript
    8. [Coinvestigate mode: wait for scientist feedback]
```

At completion, SHANDY generates a final report synthesizing all findings.

---

## Key Features

### Investigation Modes

**Autonomous Mode** (default)
- Runs all N iterations without human intervention
- Suitable for overnight or background runs
- Agent makes all decisions independently

**Coinvestigate Mode**
- Pauses after each iteration to await scientist input
- Scientist can provide guidance, redirect focus, or ask questions
- Auto-continues after 15 minutes if no feedback received
- Enables human-AI collaborative discovery

### Multi-Provider Support

SHANDY supports multiple LLM providers:

| Provider | Use Case |
|----------|----------|
| **Vertex AI** | Google Cloud with budget controls |
| **CBORG** | Lawrence Berkeley Lab's Claude API proxy |
| **Bedrock** | AWS (work in prorgress) |

Each provider has its own cost tracking and budget enforcement.

### Skills System

Skills are modular packages of domain expertise that guide the agent's reasoning:

**Workflow Skills** (domain-agnostic):
- `hypothesis-generation` - How to formulate testable hypotheses
- `result-interpretation` - How to interpret statistical results
- `prioritization` - How to decide what to investigate next
- `stopping-criteria` - When to stop investigating

**Domain Skills** (domain-specific):
- `metabolomics` - Pathway analysis, flux calculations
- `genomics` - Differential expression, enrichment analysis
- `structural-biology` - Structure validation, AlphaFold interpretation
- `data-science` - General statistical analysis

Community-contributed domain skills are being developed in the [open-science-skills](https://github.com/justaddcoffee/open-science-skills) repository. Pluggable skill installation is planned for a future release.

### MCP Tools

The agent interacts with the scientific environment through MCP (Model Context Protocol) tools:

| Tool | Purpose |
|------|---------|
| `execute_code` | Run Python code for data analysis |
| `search_pubmed` | Search PubMed for relevant papers |
| `update_knowledge_state` | Record a confirmed finding |
| `save_iteration_summary` | Save summary of what was done |
| `run_phenix_tool`* | Run Phenix structural biology tools |
| `compare_structures`* | Compare protein structures |

*Phenix tools require optional Phenix installation.

### Provenance and Reproducibility

Every action is logged for reproducibility:
- **Transcripts**: Full agent conversation for each iteration
- **Analysis log**: All code executed with outputs
- **Knowledge state**: Structured record of findings
- **Visualizations**: All generated plots with metadata

---

## Web Interface

SHANDY provides a NiceGUI-based web interface:

- **Job submission**: Upload data, enter research question, configure parameters
- **Progress monitoring**: Live status updates, iteration timeline
- **Results viewing**: Findings, visualizations, literature reviewed
- **Report download**: Final report as Markdown or PDF
- **Cost tracking**: Provider-specific spend monitoring

The timeline view uses **progressive disclosure** - showing high-level summaries with expandable details for each iteration.

---

## Supported Data Formats

SHANDY has been tested with various scientific file formats:

| Category | Tested Formats |
|----------|----------------|
| Tabular | CSV, TSV, Excel (.xlsx), Parquet, JSON |
| Structural | PDB, mmCIF |
| Images | PNG, JPG, TIFF |
| Sequence | FASTA |

The agent is generally good at understanding data in many formats beyond those listed here. Data files are optional - SHANDY can also run **literature-only investigations**.

---

## Deployment

SHANDY runs as a Docker container:

```bash
make build            # Build the Docker image
make build-no-cache   # Build without cache (for dependency updates)
make start            # Start the container
make restart          # Restart without rebuilding
```

Access the web UI at `http://localhost:8080`.

Configuration is via environment variables (`.env` file):
- Provider credentials (CBORG token, GCP credentials, etc.)
- Budget limits
- Authentication settings

See `docs/DEPLOYMENT.md` for detailed instructions.

---

## Future Directions

- **Pluggable skills**: Install community-contributed domain skills from open-science-skills
- **Alternative agents**: Support for other agentic frameworks beyond Claude Code
- **Swarm mode**: Multiple specialized agents working in parallel
- **Interactive steering**: Real-time guidance during autonomous runs
- **Experiment design**: Agent proposes follow-up experiments
- **Multi-omics integration**: Combine multiple data types in one investigation
- **Benchmark validation**: Evaluation against scientific discovery benchmarks

---

## References

- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- [open-science-skills Repository](https://github.com/justaddcoffee/open-science-skills)
