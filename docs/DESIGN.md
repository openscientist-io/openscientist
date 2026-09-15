# OpenScientist: Scientific Hypothesis Agent for Novel Discovery

## Design Document

**Status:** Implemented
**Last Updated:** August 2026

---

## What is OpenScientist?

OpenScientist is an **autonomous AI scientist** that discovers mechanistic insights from scientific data through iterative hypothesis testing. Given a research question and optional data files, OpenScientist:

1. Explores the data to understand its structure and patterns
2. Searches scientific literature (PubMed) for domain knowledge
3. Generates and tests hypotheses using statistical analysis
4. Records findings with supporting evidence and visualizations
5. Produces a final report synthesizing all discoveries

OpenScientist runs **autonomously for N iterations**, making its own decisions about what to investigate next. It can also operate in **coinvestigate mode**, pausing after each iteration to receive guidance from a human scientist.

---

## Core Design Philosophy

### Domain-Agnostic

OpenScientist is not tied to any specific scientific domain. It works with:

- Genomics and transcriptomics
- Proteomics
- Structural biology (protein structures)
- Metabolomics data
- General tabular scientific data
- Literature-only investigations (no data files required)

Domain-specific knowledge is provided through a **skills system** rather than hard-coded logic.

### Code-Writing Agent

Rather than providing pre-defined statistical functions, OpenScientist writes Python code to analyze data. This gives it:

- **Flexibility**: Can invent novel analyses on the fly
- **Transparency**: All analysis code is logged and reproducible
- **Autonomy**: Not limited to anticipated use cases

### Literature-Grounded Discovery

OpenScientist proactively searches PubMed to inform hypothesis generation and interpret results. Literature provides:

- Mechanistic context for observations
- Known pathways and regulatory relationships
- Validation of unexpected findings

### Backend-Agnostic Agent Layer

The reasoning engine is an *agentic coding assistant*, but the orchestrator does not depend on any particular one. Agents are addressed through an abstract interface, so the same discovery loop runs on more than one agent runtime and across several model providers.

---

## Technical Architecture

### Component Overview

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Host / Docker Compose                                                │
│                                                                      │
│  ┌────────────────────────────┐      ┌────────────────────────────┐  │
│  │ openscientist (web)        │      │ postgres:18                │  │
│  │  - NiceGUI UI + FastAPI    │─────▶│  - Jobs, users, findings   │  │
│  │  - JobManager (queue)      │      │  - Row-Level Security      │  │
│  │  - Auth / OAuth / sessions │      │  - Alembic-managed schema  │  │
│  └───────────┬────────────────┘      └────────────────────────────┘  │
│              │ DOCKER_HOST=tcp://docker-socket-proxy:2375            │
│              ▼                                                       │
│  ┌────────────────────────────┐                                      │
│  │ docker-socket-proxy        │  the ONLY container that mounts      │
│  │  (restricted Docker API)   │  /var/run/docker.sock                │
│  └───────────┬────────────────┘                                      │
│              │ create / start / stop / wait / logs / remove          │
│              ▼                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ openscientist-agent  (one ephemeral container PER JOB)       │    │
│  │                                                              │    │
│  │   agent-entrypoint.py                                        │    │
│  │     └─▶ orchestrator.discovery                               │    │
│  │           └─▶ AbstractAgent  (ClaudeCodeAgent | CodexAgent)  │    │
│  │                 │                                            │    │
│  │                 │ stdio MCP                                  │    │
│  │                 ▼                                            │    │
│  │           python -m openscientist_tools   (subprocess)       │    │
│  │                 │                                            │    │
│  └─────────────────┼────────────────────────────────────────────┘    │
│                    │ execute_code spawns a further container         │
│                    ▼                                                 │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ openscientist-executor  (per code execution, read-only FS)   │    │
│  └──────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

### The Agent Abstraction

`AbstractAgent` (`src/openscientist/agent/base.py`) is the backend-agnostic interface the orchestrator programs against. Every concrete agent declares which runtime it is via a class-level `AgentBackend`:

| Backend enum | Value | Agent class |
|---|---|---|
| `AgentBackend.CLAUDE_CODE` | `claude_code` | `ClaudeCodeAgent` (`agent/claude_code_agent.py`) |
| `AgentBackend.CODEX` | `codex` | `CodexAgent` (`agent/codex_agent.py`) |

Supporting types in the same module include `AgentConfig` (job dir, optional data file, system prompt, hypothesis flag) and `IterationResult` (outcome, output, tool-call count, typed transcript).

`AbstractAgent` also defines `provision_host_prelaunch()`, a classmethod hook that runs in the web/orchestrator process *before* the agent container starts, for backends that need host-side auth provisioning.

### Backend Selection

Backend choice is derived from the configured provider, not set separately. `src/openscientist/agent/factory.py` performs the single dispatch:

- A provider implementing `ClaudeCompatible` → `ClaudeCodeAgent`
- A provider implementing `CodexCompatible` → `CodexAgent`

`get_agent()` resolves the provider from `settings.provider.provider_id` and builds the matching agent. `agent_class_for_provider_id()` and `backend_for_provider_id()` allow the web process to resolve a backend without instantiating (and therefore without validating) provider credentials.

### Providers

`src/openscientist/providers/__init__.py` holds a single provider registry (`_PROVIDER_CLASS_PATHS`), which the agent factory derives from — there is no second registry to keep in sync. Provider modules are imported on demand so provider SDK dependencies stay optional.

| `OPENSCIENTIST_PROVIDER` | Class | Compatibility family | Agent backend |
|---|---|---|---|
| `anthropic` | `AnthropicProvider` | `ClaudeCompatible` | Claude Code |
| `cborg` | `CborgProvider` | `ClaudeCompatible` | Claude Code |
| `vertex` | `VertexProvider` | `ClaudeCompatible` | Claude Code |
| `bedrock` | `BedrockProvider` | `ClaudeCompatible` | Claude Code |
| `foundry` | `FoundryProvider` | `ClaudeCompatible` | Claude Code |
| `openai` | `OpenAIDirectProvider` | `CodexCompatible` | Codex |
| `azure-openai` | `AzureOpenAIProvider` | `CodexCompatible` | Codex |
| `ollama` | `OllamaProvider` | `CodexCompatible` | Codex |

`OPENSCIENTIST_PROVIDER` is required and has no default; an unset value raises at startup. Each provider validates its own credentials on construction, contributes the environment the agent CLI needs, and (where the platform offers a billing API) reports cost information.

Per-provider credential variables are documented in [`.env.example`](../.env.example), which is the canonical reference for configuration.

### Tools: the `openscientist_tools` MCP Server

Agent tools live in a **separate top-level package**, `src/openscientist_tools/`, not inside the `openscientist` package. It is a FastMCP server (`openscientist_tools/server.py`) with an executable entry point (`openscientist_tools/__main__.py`).

The agent runs it as a **stdio subprocess MCP server**, not as a long-running sidecar and not in-process:

- `ClaudeCodeAgent` declares a `StdioMcpServerSpec` (`agent/mcp_specs.py`) invoking `python -m openscientist_tools`, and the Claude Agent SDK manages the subprocess lifecycle.
- `CodexAgent` writes the equivalent `mcp_servers` entry (`args = ["-m", "openscientist_tools"]`) into the codex configuration it generates.

Subprocess environment is assembled by the agent (for the Claude backend, `_build_subprocess_env()`): the container environment is inherited and per-job values are overlaid. Because the subprocess inherits `DOCKER_HOST`, its `execute_code` tool can spawn executor containers through the same restricted proxy the rest of the system uses.

### Tool Inventory

Registered in `src/openscientist_tools/`:

| Tool | Module | Purpose |
|---|---|---|
| `execute_code` | `code_exec.py` | Run Python code for data analysis |
| `search_pubmed` | `pubmed.py` | Search PubMed for relevant papers |
| `read_document` | `document.py` | Extract text from PDF, Word, and Excel files |
| `update_knowledge_state` | `knowledge.py` | Record a confirmed finding |
| `add_hypothesis`* | `knowledge.py` | Register a hypothesis before testing |
| `update_hypothesis`* | `knowledge.py` | Record a hypothesis outcome |
| `save_iteration_summary` | `job_meta.py` | Save a summary of the iteration |
| `set_status` | `job_meta.py` | Update the status message shown in the UI |
| `set_job_title` | `job_meta.py` | Set the job's display title |
| `set_consensus_answer` | `job_meta.py` | Record a direct answer to the research question |
| `run_phenix_tool`† | `phenix.py` | Run Phenix structural biology tools |
| `compare_structures`† | `phenix.py` | Compare protein structures |
| `parse_alphafold_confidence`† | `phenix.py` | Extract AlphaFold pLDDT confidence metrics |
| `ping` | `server.py` | Round-trip smoke tool |

\* Registered when hypothesis tracking is enabled for the job.
† Registered when Phenix is available (`PHENIX_PATH` probe).

### Container-per-Job Isolation

`JobManager` does not run the discovery loop in-process. `JobContainerRunner` (`src/openscientist/job_container/runner.py`) launches one ephemeral **agent container** per job from the `openscientist-agent` image, applying `no-new-privileges:true`, a memory limit, a CPU limit, and a resolved Docker network. Job configuration is passed as container environment, including `OPENSCIENTIST_RUN_MODE` for non-default run modes.

Inside the container, `docker/agent-entrypoint.py` reads `JOB_ID`, `JOB_DIR`, and `OPENSCIENTIST_RUN_MODE`, then calls the orchestrator directly.

When the agent executes code, `ContainerManager` (`src/openscientist/container_manager.py`) starts a further **executor container** from the `openscientist-executor` image with `read_only: True`, `no-new-privileges:true`, and memory/CPU limits.

### Docker Socket Proxy

No application container mounts the host Docker socket. `docker-compose.yml` runs a `docker-socket-proxy` service that is the only container with `/var/run/docker.sock` (mounted read-only); the web app and agent containers reach the Docker API over `DOCKER_HOST=tcp://docker-socket-proxy:2375`.

The proxy whitelists only what the job lifecycle needs — containers (list/inspect/logs/create/start/stop/wait/remove) and images (inspect/pull), plus ping and version negotiation. `EXEC`, `INFO`, `NETWORKS`, `VOLUMES`, `BUILD`, `SWARM`, `SECRETS` and the rest are explicitly denied.

This substantially narrows the attack surface an unrestricted socket mount exposes, but it does not eliminate it. Container creation is necessarily permitted, and the proxy filters by API path and HTTP method rather than by request body, so a compromised application container could still create a container with arbitrary bind mounts or privileged options and reach the host that way. The control is a meaningful reduction in reachable API surface, not a container-escape boundary. See [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) for the residual risk as a tracked finding.

### Run Modes

`RunMode` (`src/openscientist/job/types.py`) defines how a launch behaves:

| Mode | Value | Behaviour |
|---|---|---|
| `DISCOVERY` | `discovery` | Full discovery loop. The default. |
| `REPORT_ONLY` | `report_only` | Re-runs only the report-generation phase against already-persisted findings. |

Report-only backs the admin "Regenerate report" action: `JobManager.regenerate_report()` launches the agent container with `OPENSCIENTIST_RUN_MODE=report_only`, and the entrypoint routes to `regenerate_report_async()` instead of the discovery loop. The job must exist, be completed, not be running, and a concurrency slot must be free.

### Persistence: PostgreSQL

PostgreSQL is the system of record for application state — users, sessions, OAuth accounts, API keys, jobs, shares, findings, hypotheses, literature, analysis logs, chat messages, cost records, and skills. Models live in `src/openscientist/database/models/`.

**Row-Level Security.** RLS policies are enabled on the sensitive tables so the database itself prevents cross-user data access even if application code has a bug. Two roles are provisioned: `openscientist_app` for normal access and `openscientist_admin` with `BYPASSRLS` for admin and background operations (used by `get_admin_session`). The roles are created by `docker/postgres/init.sql` on a fresh container and by the initial migration.

**Alembic migrations.** Schema changes are forward-only Alembic revisions under `src/openscientist/database/migrations/versions/`. Applied migration history is treated as immutable. See the [migrations README](../src/openscientist/database/migrations/README.md) for the current revision chain and the policy.

The per-job **knowledge state** JSON remains the agent's working record inside the job directory; the database holds the durable, queryable application state.

### Transcripts

Agent conversations are persisted as **typed transcript entries** rather than raw SDK payloads. `src/openscientist/transcript/` defines the schema (`variants/`, `union.py`), per-backend translators (`translators/claude.py`, `translators/codex.py`), and load/save helpers (`io.py`). This is what lets one webapp timeline render output from either agent backend.

Job directories created before the typed format landed are not readable by the webapp or `load_transcript` until migrated; see [`AGENT_ABSTRACTION_DEPLOYMENT.md`](AGENT_ABSTRACTION_DEPLOYMENT.md) for the one-off migration script.

### The Discovery Loop

Each job runs through multiple iterations:

```text
For each iteration (1 to N):
    1. Agent receives current knowledge state + prompt
    2. Agent decides what to investigate
    3. Agent calls tools (execute code, search literature, etc.)
    4. MCP subprocess executes tools, returns results
    5. Agent interprets results, records findings
    6. Agent saves iteration summary
    7. Orchestrator increments iteration, saves transcript
    8. [Coinvestigate mode: wait for scientist feedback]
```

At completion, OpenScientist generates a final report synthesizing all findings.

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
- Auto-continues after 15 minutes if no feedback is received (`FEEDBACK_TIMEOUT_SECONDS`, `orchestrator/iteration.py`)
- Enables human-AI collaborative discovery

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
- `berkeley-data-lakehouse` - Query Berkeley Lab scientific data repositories

Built-in skills live in `skills/` at the repository root. Each backend materialises the enabled skills into the job workspace in its own layout (`agent/skills.py`): the Claude backend writes `.claude/CLAUDE.md` plus `.claude/skills/*.md`, and the Codex backend writes `.agents/skills/*/SKILL.md`.

Community-contributed domain skills are being developed in the [open-science-skills](https://github.com/justaddcoffee/open-science-skills) repository.

### Cost Tracking

Two distinct mechanisms:

- **Provider spend APIs** — each provider may implement `get_cost_info()` against its platform's billing API (for example AWS Cost Explorer, GCP BigQuery billing export, Azure Cost Management). Availability and data lag vary by provider.
- **Per-job token cost estimation** — `src/openscientist/providers/pricing.py` estimates job cost from token counts using the litellm pricing database, falling back to a small built-in rate table when the remote fetch is unavailable. Model ids are normalised first, so Bedrock- and Vertex-style ids resolve to their base pricing keys. Results are persisted as cost records.

Budget limits are configurable and checked before job creation; see the budget variables in [`.env.example`](../.env.example).

### Provenance and Reproducibility

Every action is logged for reproducibility:

- **Transcripts**: Full agent conversation for each iteration, in the typed transcript format
- **Analysis log**: All code executed with outputs
- **Knowledge state**: Structured record of findings
- **Visualizations**: All generated plots with metadata

---

## Web Interface

OpenScientist provides a NiceGUI-based web interface backed by FastAPI:

- **Job submission**: Upload data, enter research question, configure parameters
- **Progress monitoring**: Live status updates, iteration timeline
- **Results viewing**: Findings, visualizations, literature reviewed
- **Report download**: Final report as Markdown or PDF
- **Cost tracking**: Provider-specific spend monitoring
- **Sharing**: Explicit per-job sharing at view or edit permission levels
- **Admin**: User approval, orphaned-job assignment, report regeneration

The timeline view uses **progressive disclosure** - showing high-level summaries with expandable details for each iteration.

Authentication is OAuth-based (Google, GitHub, ORCID, plus a mock provider for development), with database-backed sessions. A REST API is available for programmatic access using API keys.

---

## Supported Data Formats

OpenScientist has been tested with various scientific file formats:

| Category | Tested Formats |
|----------|----------------|
| Tabular | CSV, TSV, Excel (.xlsx), Parquet, JSON |
| Structural | PDB, mmCIF |
| Images | PNG, JPG, TIFF |
| Sequence | FASTA |
| Documents | PDF, DOCX (via `read_document`) |

The agent is generally good at understanding data in many formats beyond those listed here. Data files are optional - OpenScientist can also run **literature-only investigations**.

---

## Deployment

Local development and single-host deployment use Docker Compose:

```bash
make build            # Build base, main, agent, and executor images
make start            # Start the stack
make rebuild          # Rebuild images and restart
```

Access the web UI at `http://localhost:8080`.

Configuration is via environment variables (`.env`); [`.env.example`](../.env.example) documents the full set.

For required configuration, the database and migration steps, the container topology, and the CI/CD-driven multi-environment deployment, see [`DEPLOYMENT.md`](DEPLOYMENT.md), [`ENVIRONMENTS.md`](ENVIRONMENTS.md), and [`CICD.md`](CICD.md).

---

## Future Directions

- **Pluggable skills**: Install community-contributed domain skills from open-science-skills
- **Swarm mode**: Multiple specialized agents working in parallel
- **Interactive steering**: Real-time guidance during autonomous runs
- **Experiment design**: Agent proposes follow-up experiments
- **Multi-omics integration**: Combine multiple data types in one investigation
- **Benchmark validation**: Evaluation against scientific discovery benchmarks

---

## References

- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
- [open-science-skills Repository](https://github.com/justaddcoffee/open-science-skills)
