# SHANDY: Scientific Hypothesis Agent for Novel Discovery
## Autonomous Loop Architecture Design

**Date:** 2025-11-13
**Status:** Design Complete
**Goal:** Build a practical, domain-agnostic autonomous scientific discovery system

---

## Overview

**SHANDY** is an autonomous scientific discovery agent that:
1. Accepts data files and a research question
2. Runs for N iterations autonomously (hours, not minutes)
3. Generates hypotheses, tests them, searches literature
4. Produces a final report with findings and mechanistic insights
5. Can be validated by domain experts (not exact statistical reproduction)

**Domain:** Initially metabolomics, but designed to be domain-agnostic

---

## Design Principles

1. **Domain-Agnostic**: Works for metabolomics, genomics, proteomics, climate data, etc.
2. **Single Agent First**: One autonomous agent making decisions, not a swarm (but architected to allow future swarm conversion)
3. **Maximum Flexibility**: Agent writes Python code for analyses (not pre-defined statistical functions)
4. **Literature-Grounded**: Proactively searches papers to inform hypothesis generation
5. **Practical**: Fixed iteration budget, cost monitoring, Docker deployment
6. **Skills-Based**: Modular skills architecture for extensibility

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│  DOCKER CONTAINER                                        │
│                                                          │
│  ┌────────────────────────────────────────────────┐    │
│  │ NiceGUI Web UI (app.py)                         │    │
│  │ - Job creation & file upload                    │    │
│  │ - Progress monitoring (polling)                 │    │
│  │ - Result viewing & download                     │    │
│  │ - Cost tracking display                         │    │
│  └────────────────────────────────────────────────┘    │
│                      ↓                                   │
│  ┌────────────────────────────────────────────────┐    │
│  │ Job Manager (job_manager.py)                    │    │
│  │ - Spawns background processes                   │    │
│  │ - Tracks job status                             │    │
│  │ - Monitors costs via CBORG API                  │    │
│  └────────────────────────────────────────────────┘    │
│                      ↓                                   │
│  ┌────────────────────────────────────────────────┐    │
│  │ Orchestrator (orchestrator.py)                  │    │
│  │ - Main discovery loop (max N iterations)        │    │
│  │ - Calls Claude API with context                 │    │
│  │ - Updates knowledge graph JSON                  │    │
│  │ - Loads domain-specific skills                  │    │
│  └────────────────────────────────────────────────┘    │
│         ↓              ↓              ↓                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐         │
│  │Code Exec │  │Literature│  │Knowledge Graph│         │
│  │(sandbox) │  │(PubMed)  │  │  (JSON)       │         │
│  │Python    │  │API       │  │  State        │         │
│  └──────────┘  └──────────┘  └──────────────┘         │
│                                                          │
└─────────────────────────────────────────────────────────┘
     ↑ mount           ↓ output
  /data            /jobs
```

**User workflow:**
```bash
# Start SHANDY container
docker run -p 8602:8602 \
           -v ./my-data:/data \
           -v ./results:/jobs \
           -e ANTHROPIC_AUTH_TOKEN=$CBORG_API_KEY \
           shandy

# Access web UI
open http://localhost:8602

# Upload data, enter research question, click "Start Analysis"
# Agent runs for hours autonomously
# Check back later to view findings
```

---

## Core Components

### 1. Discovery Loop (orchestrator.py)

**Main autonomous loop:**

```python
def autonomous_discovery(job_id, research_question, data_files, max_iterations):
    """
    Main autonomous discovery loop

    Args:
        job_id: Unique job identifier
        research_question: User's scientific question
        data_files: List of uploaded data file paths
        max_iterations: Budget (e.g., 50 iterations)

    Returns:
        Final report with findings
    """
    # Initialize
    data = load_and_validate_data(data_files)
    domain = detect_domain(data)  # Infer from column names, file structure
    skills = load_skills(domain)  # Load workflow/ + domain-specific skills

    # Track costs
    start_spend = get_cborg_spend()

    # Knowledge graph (JSON state)
    knowledge_graph = {
        "config": {
            "research_question": research_question,
            "max_iterations": max_iterations,
            "started_at": datetime.now().isoformat()
        },
        "data_summary": summarize_data(data),
        "iteration": 0,
        "hypotheses": [],
        "findings": [],
        "literature": [],
        "analysis_log": []
    }

    # Bootstrap: Initial literature survey
    initial_papers = search_literature(research_question)
    knowledge_graph["literature"] = initial_papers

    # Discovery loop
    for iteration in range(max_iterations):
        knowledge_graph["iteration"] = iteration

        # 1. Prompt Claude with current state
        prompt = build_discovery_prompt(
            knowledge_graph=knowledge_graph,
            data_summary=knowledge_graph["data_summary"],
            skills=skills,
            iteration=iteration,
            max_iterations=max_iterations
        )

        # 2. Call Claude API (with tools)
        response = call_claude_api(
            system=get_system_prompt(skills),
            messages=[{"role": "user", "content": prompt}],
            tools=[
                execute_code,      # Primary tool: run Python analysis
                search_pubmed,     # Literature search
                use_skill,         # Invoke a skill workflow
                update_kg          # Record a finding
            ]
        )

        # 3. Execute tool calls
        results = execute_tool_calls(response.tool_calls, data, knowledge_graph)

        # 4. Update knowledge graph
        knowledge_graph = update_state(knowledge_graph, results)

        # 5. Persist checkpoint
        save_checkpoint(job_id, knowledge_graph)

        # 6. Check cost limits
        current_spend = get_cborg_spend()
        job_cost = current_spend - start_spend
        if job_cost > get_max_job_cost():
            logger.warning(f"Job exceeded budget: ${job_cost:.2f}")
            break

        # 7. Check early stopping
        if should_stop_early(knowledge_graph):
            break

    # Generate final report
    report = synthesize_findings(knowledge_graph)
    save_final_report(job_id, report)

    return {
        "job_id": job_id,
        "total_iterations": iteration + 1,
        "cost_usd": job_cost,
        "findings_count": len(knowledge_graph["findings"]),
        "report_path": f"/jobs/{job_id}/final_report.md"
    }
```

**Key decisions:**
- **Termination:** Fixed iteration budget (practical, predictable)
- **State management:** JSON knowledge graph (simple, debuggable)
- **Tool calling:** Claude decides which tools to use each iteration

---

### 2. Code Execution Tool (code_executor.py)

**Primary analysis mechanism: Agent writes Python code**

```python
def execute_code(code: str, data: pd.DataFrame, timeout: int = 60):
    """
    Execute Python code in sandboxed environment

    Security:
    - Timeout: 60 seconds max
    - Import whitelist: pandas, numpy, scipy, matplotlib, seaborn, statsmodels, sklearn
    - No network access (except via explicit tools)
    - No file system access outside /workspace
    - Runs inside Docker container

    Args:
        code: Python code to execute
        data: DataFrame available as `data` variable
        timeout: Max execution time in seconds

    Returns:
        {
            "success": bool,
            "output": str,        # Printed output
            "plots": [paths],     # Generated plot files
            "error": str,         # If failed
            "execution_time": float
        }
    """
    # Validate imports
    allowed_imports = ['pandas', 'numpy', 'scipy', 'matplotlib',
                       'seaborn', 'statsmodels', 'sklearn']
    if not validate_imports(code, allowed_imports):
        return {"success": False, "error": "Forbidden import detected"}

    # Execute in isolated namespace
    namespace = {
        'data': data,
        'pd': pandas,
        'np': numpy,
        # ... other allowed libraries
    }

    try:
        # Run with timeout
        with timeout_context(timeout):
            exec(code, namespace)

        # Collect results
        return {
            "success": True,
            "output": capture_stdout(),
            "plots": collect_generated_plots(),
            "execution_time": elapsed_time
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "traceback": format_exc()
        }
```

**Why code execution instead of pre-defined functions?**
- **Flexibility:** Agent can invent novel analyses
- **Domain-agnostic:** Works for any data type
- **Autonomy:** Not limited to metabolomics-specific operations
- **Example:** Agent can create custom flux indices, ratios, transformations on the fly

---

### 3. Knowledge Graph Schema (knowledge_graph.py)

**JSON structure for agent memory:**

```json
{
  "config": {
    "research_question": "Why is hypothermia neuroprotective?",
    "max_iterations": 50,
    "started_at": "2025-11-13T15:30:00Z",
    "domain": "metabolomics"
  },

  "data_summary": {
    "files": ["Brain_Polar_C_ST_NT_245_2024.csv"],
    "n_samples": 17,
    "n_features": 245,
    "groups": ["Control", "Hypothermia", "Normothermia"],
    "feature_types": ["metabolite_abundance"]
  },

  "iteration": 15,

  "hypotheses": [
    {
      "id": "H001",
      "iteration_proposed": 3,
      "statement": "Nucleotide salvage flux is increased in hypothermia",
      "status": "rejected",
      "tested_at_iteration": 5,
      "test_code": "# Python code that was executed...",
      "result": {
        "p_value": 0.138,
        "effect_size": null,
        "conclusion": "No significant difference in salvage flux"
      },
      "spawned_hypotheses": ["H002", "H003"]
    },
    {
      "id": "H003",
      "iteration_proposed": 6,
      "statement": "CMP→CDP-choline conversion is rate-limited in hypothermia",
      "status": "supported",
      "tested_at_iteration": 8,
      "test_code": "...",
      "result": {
        "p_value": 0.042,
        "effect_size": 0.29,
        "conclusion": "CDP-Choline Synthesis Index 35.4% higher"
      },
      "literature_support": ["L005"]
    }
  ],

  "findings": [
    {
      "id": "F001",
      "iteration_discovered": 8,
      "title": "CMP→CDP-choline bottleneck in hypothermia",
      "evidence": "CDP-Choline Synthesis Index 35.4% higher (p=0.042, η²=0.29)",
      "supporting_hypotheses": ["H003"],
      "literature_support": ["L005", "L008"],
      "plots": ["plots/cdp_choline_synthesis_index.png"],
      "biological_interpretation": "Pcyt1 enzyme may be rate-limiting"
    }
  ],

  "literature": [
    {
      "id": "L005",
      "pmid": "12345678",
      "title": "Pcyt1 regulation in neuronal metabolism",
      "key_finding": "Pcyt1 is rate-limiting for CDP-choline synthesis",
      "relevance_to": ["F001", "H003"],
      "retrieved_at_iteration": 6,
      "search_query": "Pcyt1 CDP-choline brain"
    }
  ],

  "analysis_log": [
    {
      "iteration": 5,
      "action": "execute_code",
      "code": "# Python analysis code...",
      "output": "...",
      "execution_time_sec": 2.3,
      "plots_generated": ["plots/iter5_volcano.png"]
    },
    {
      "iteration": 6,
      "action": "search_pubmed",
      "query": "Pcyt1 CDP-choline",
      "results_count": 15,
      "top_pmids": ["12345678", "87654321"]
    }
  ]
}
```

**Future enhancement:** Define LinkML schema for validation and semantic interoperability

---

### 4. Skills Architecture

**Optional skills system with user toggle**

**Configuration (.env):**
```bash
# Skills configuration
SKILLS_ENABLED=true              # Global default
ALLOW_SKILLS_TOGGLE=true         # Let users override per-job
```

**Domain-agnostic workflows + domain-specific knowledge**

```
.claude/skills/
├── workflow/                      # How to do science (domain-agnostic)
│   ├── hypothesis-generation/
│   │   └── SKILL.md              # How to formulate testable hypotheses
│   ├── result-interpretation/
│   │   └── SKILL.md              # How to interpret positive/negative results
│   ├── prioritization/
│   │   └── SKILL.md              # How to decide what to test next
│   └── stopping-criteria/
│       └── SKILL.md              # When to stop investigating
│
└── domain/                        # Domain-specific knowledge
    ├── metabolomics/
    │   ├── statistical-tests/     # What tests metabolomics uses
    │   ├── pathway-analysis/      # How to map metabolites to pathways
    │   ├── quality-control/       # Metabolomics-specific QC
    │   └── visualization/         # How to visualize metabolomics data
    │
    ├── genomics/
    │   ├── statistical-tests/     # Different tests than metabolomics!
    │   ├── variant-calling/
    │   └── ...
    │
    └── data-science/              # General scientific computing
        ├── data-loading/
        ├── exploratory-analysis/
        └── plotting-basics/
```

**Example skill: `workflow/hypothesis-generation/SKILL.md`**

```markdown
---
name: hypothesis-generation
description: Generate testable hypotheses from data patterns and literature
---

# Hypothesis Generation

## When to Use
- After identifying an interesting pattern in the data
- When a previous hypothesis was rejected (generate alternatives)
- At the start of investigation (bootstrap from literature)

## Process

1. **Review current knowledge**
   - What patterns have been observed?
   - What hypotheses have been tested (and their results)?
   - What does literature say about this domain?

2. **Identify unexplained patterns**
   - What group differences exist?
   - What correlations are surprising?
   - What contradicts expectations?

3. **Search literature for mechanisms**
   - Query PubMed with pattern + domain keywords
   - Extract known biological mechanisms
   - Identify enzymes, pathways, regulatory factors

4. **Formulate specific, testable hypotheses**
   - State as: "X causes Y via mechanism Z"
   - Must be testable with available data
   - Should be falsifiable

5. **Prioritize hypotheses**
   - Impact: How central to research question?
   - Feasibility: Can we test with current data?
   - Novelty: Is this a new insight?
   - Coherence: Does it explain other findings?

## Output
- List of hypotheses ranked by priority
- Suggested statistical tests for each
- Literature references supporting each hypothesis
```

**Skill invocation by agent:**

```python
# Agent calls use_skill() tool
result = use_skill(
    skill_name="hypothesis-generation",
    context={
        "observed_pattern": "CMP elevated in hypothermia",
        "rejected_hypotheses": ["H001: Salvage flux increased"],
        "literature_context": [...]
    }
)
# Returns: Ranked list of new hypotheses to test
```

**Skills Toggle Implementation:**

Users can choose whether to use skills on a per-job basis:

```python
# In UI (app.py)
use_skills_checkbox = ui.checkbox(
    "Use skills (structured workflows)",
    value=True
).tooltip(
    "Enabled: Agent follows structured discovery workflows\n"
    "Disabled: Pure LLM reasoning, more exploratory"
)

# In orchestrator
def autonomous_discovery(job_id, research_question, data_files, max_iterations, use_skills=True):
    if use_skills:
        domain = detect_domain(data)
        skills = load_skills(domain)
        tools = [execute_code, search_pubmed, use_skill, update_kg]
    else:
        skills = None
        tools = [execute_code, search_pubmed, update_kg]  # No use_skill tool

    # Different system prompts based on mode
    system_prompt = get_system_prompt(skills_enabled=use_skills)
```

**Benefits:**
- Compare skills-based vs pure LLM approaches
- Maximum flexibility for different research questions
- Debug skills by disabling them
- User preference for guidance vs exploration

---

### 5. Literature Integration (literature.py)

**Proactive literature search to inform hypotheses**

```python
def search_pubmed(query: str, max_results: int = 10):
    """
    Search PubMed and return relevant papers

    Args:
        query: Search terms (e.g., "hypothermia neuroprotection metabolomics")
        max_results: Number of papers to return

    Returns:
        List of papers with abstracts
    """
    # Use PubMed E-utilities API
    response = requests.get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json"
        }
    )
    pmids = response.json()["esearchresult"]["idlist"]

    # Fetch abstracts
    papers = []
    for pmid in pmids:
        abstract = fetch_abstract(pmid)
        papers.append({
            "pmid": pmid,
            "title": abstract["title"],
            "abstract": abstract["text"],
            "authors": abstract["authors"],
            "year": abstract["year"]
        })

    return papers


def extract_mechanism_from_papers(papers: list, metabolite: str):
    """
    Use Claude to extract mechanistic knowledge from abstracts

    Args:
        papers: List of paper dicts from search_pubmed()
        metabolite: Metabolite of interest

    Returns:
        Mechanistic insights extracted from literature
    """
    # Concatenate abstracts
    context = "\n\n".join([
        f"PMID {p['pmid']}: {p['title']}\n{p['abstract']}"
        for p in papers
    ])

    # Ask Claude to extract mechanisms
    prompt = f"""
    Based on these papers, what is known about {metabolite} regulation
    and its role in the biological context?

    {context}

    Extract:
    1. Key enzymes/pathways involving {metabolite}
    2. Known regulatory mechanisms
    3. Associations with phenotypes/diseases
    """

    response = call_claude_api(prompt)
    return response
```

**When literature is searched:**
- **Bootstrap:** At iteration 0, survey the research domain
- **Hypothesis generation:** When agent needs mechanistic ideas
- **Result interpretation:** When finding is surprising/unexplained

---

### 6. Cost Monitoring (cost_tracker.py)

**Track spending via CBORG API**

```python
def get_cborg_spend():
    """Query CBORG API for current spend"""
    response = requests.get(
        "https://api.cborg.lbl.gov/key/info",
        headers={"Authorization": "Bearer " + os.environ.get("ANTHROPIC_AUTH_TOKEN")}
    )
    return response.json()["info"]["spend"]


def get_budget_info():
    """
    Get budget information from CBORG and application settings

    Returns:
        {
            "current_spend": float,        # Current CBORG spend
            "cborg_max_budget": float|None, # CBORG budget limit (if set)
            "budget_remaining": float|None, # CBORG budget remaining
            "app_max_job_cost": float,     # Per-job limit from .env
            "app_max_total_budget": float  # Total app budget from .env
        }
    """
    response = requests.get(
        "https://api.cborg.lbl.gov/key/info",
        headers={"Authorization": "Bearer " + os.environ.get("ANTHROPIC_AUTH_TOKEN")}
    )
    info = response.json()["info"]

    current_spend = info["spend"]
    cborg_budget = info.get("max_budget")  # May be None

    # Application-level limits from .env
    app_max_job = float(os.getenv("MAX_JOB_COST_USD", "10.0"))
    app_max_total = float(os.getenv("APP_MAX_BUDGET_USD", "1000.0"))

    return {
        "current_spend": current_spend,
        "cborg_max_budget": cborg_budget,
        "budget_remaining": cborg_budget - current_spend if cborg_budget else None,
        "app_max_job_cost": app_max_job,
        "app_max_total_budget": app_max_total,
        "key_expires": info["expires"]
    }


def check_budget_before_job(estimated_cost: float = 5.0):
    """
    Check if we have enough budget to run a job

    Raises ValueError if insufficient budget
    """
    budget_info = get_budget_info()

    # Check CBORG budget (if set)
    if budget_info["cborg_max_budget"]:
        if budget_info["budget_remaining"] < estimated_cost:
            raise ValueError(
                f"Insufficient CBORG budget: "
                f"${budget_info['budget_remaining']:.2f} remaining, "
                f"need ~${estimated_cost}"
            )

    # Check application-level limit
    if budget_info["current_spend"] + estimated_cost > budget_info["app_max_total_budget"]:
        raise ValueError(
            f"Would exceed app budget limit of ${budget_info['app_max_total_budget']}"
        )


def track_job_cost(job_id: str, iteration: int):
    """Update job metadata with current cost"""
    current_spend = get_cborg_spend()
    job_metadata = load_job_metadata(job_id)

    job_cost = current_spend - job_metadata["start_spend"]

    update_job_metadata(job_id, {
        "current_cost_usd": job_cost,
        "iteration": iteration,
        "cost_per_iteration": job_cost / (iteration + 1) if iteration > 0 else 0
    })

    # Check if exceeding per-job limit
    max_job_cost = float(os.getenv("MAX_JOB_COST_USD", "10.0"))
    if job_cost > max_job_cost:
        logger.warning(f"Job {job_id} exceeded budget: ${job_cost:.2f}")
        raise BudgetExceededError(f"Job cost ${job_cost:.2f} exceeds limit ${max_job_cost}")
```

**.env configuration:**

```bash
# Cost limits
MAX_JOB_COST_USD=10.00           # Stop if single job exceeds this
APP_MAX_BUDGET_USD=1000.00       # Total application spending limit
WARN_COST_THRESHOLD_USD=5.00     # Warn user when job reaches this
```

**UI display:**

```
Account Status:
💰 CBORG Spend: $321.99 (no limit set on CBORG)
📅 Key Expires: 2026-06-13

App Limits:
⚠️  Max per job: $10.00
🔒 Total app budget: $1000.00

Current Job:
💵 Cost so far: $2.34 (15/50 iterations)
📊 Avg cost/iteration: $0.16
```

---

## Web UI (app.py)

**NiceGUI interface for job management**

```python
#!/usr/bin/env python
"""SHANDY - Scientific Hypothesis Agent for Novel Discovery"""

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from nicegui import ui, app as nicegui_app
from dotenv import load_dotenv

load_dotenv()

# Configuration from .env
PORT = int(os.getenv("PORT", "8602"))
STORAGE_SECRET = os.getenv("STORAGE_SECRET")
ANTHROPIC_AUTH_TOKEN = os.getenv("ANTHROPIC_AUTH_TOKEN")

if not ANTHROPIC_AUTH_TOKEN:
    raise ValueError("ANTHROPIC_AUTH_TOKEN must be set in .env")

from ad_swarm.job_manager import create_job, get_job_status, list_jobs
from ad_swarm.cost_tracker import get_budget_info, check_budget_before_job


@ui.page("/")
async def index():
    """Main page - job creation and management"""

    # Header
    ui.markdown("# SHANDY")
    ui.markdown("_Scientific Hypothesis Agent for Novel Discovery_")

    # Budget display
    budget_info = get_budget_info()
    with ui.card():
        ui.label("Account Status").classes("text-lg font-bold")
        ui.label(f"💰 CBORG Spend: ${budget_info['current_spend']:.2f}")
        if budget_info['cborg_max_budget']:
            ui.label(f"📊 Budget Remaining: ${budget_info['budget_remaining']:.2f}")
        ui.label(f"⚠️ Max per job: ${budget_info['app_max_job_cost']:.2f}")

    # New Job Form
    with ui.card().classes("w-full max-w-2xl"):
        ui.label("Create New Analysis Job").classes("text-xl font-bold")

        question_input = ui.textarea(
            label="Research Question",
            placeholder="e.g., Why is hypothermia neuroprotective?",
        ).classes("w-full")

        file_upload = ui.upload(
            label="Upload Data Files (CSV)",
            multiple=True,
            auto_upload=True
        ).classes("w-full")

        max_iter_input = ui.number(
            label="Max Iterations",
            value=50,
            min=1,
            max=200
        )

        # Skills toggle
        use_skills_checkbox = ui.checkbox(
            "Use skills (structured workflows)",
            value=True
        ).tooltip(
            "Enabled: Agent follows structured discovery workflows\n"
            "Disabled: Pure LLM reasoning, more exploratory"
        )

        async def start_job():
            # Validate inputs
            if not question_input.value:
                ui.notify("Please enter a research question", color="negative")
                return
            if not file_upload.value:
                ui.notify("Please upload at least one data file", color="negative")
                return

            # Check budget
            try:
                check_budget_before_job(estimated_cost=5.0)
            except ValueError as e:
                ui.notify(str(e), color="negative")
                return

            # Create job
            job_id = create_job(
                research_question=question_input.value,
                data_files=file_upload.value,
                max_iterations=int(max_iter_input.value),
                use_skills=use_skills_checkbox.value
            )

            # Start job in background
            subprocess.Popen([
                "python", "-m", "ad_swarm.orchestrator",
                "--job-id", job_id
            ])

            ui.notify(f"Analysis started! Job ID: {job_id}", color="positive")
            ui.navigate.to(f"/job/{job_id}")

        ui.button("Start Analysis", on_click=start_job).classes("w-full")

    # Running Jobs
    with ui.card().classes("w-full max-w-2xl mt-4"):
        ui.label("Running Jobs").classes("text-xl font-bold")
        jobs = list_jobs(status="running")
        if not jobs:
            ui.label("No running jobs")
        else:
            for job in jobs:
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"○ Job #{job['id']} - {job['question'][:50]}...")
                    ui.label(f"Iteration {job['iteration']}/{job['max_iterations']}")
                    ui.label(f"Cost: ${job['cost']:.2f}")
                    ui.button("View", on_click=lambda j=job: ui.navigate.to(f"/job/{j['id']}"))

    # Completed Jobs
    with ui.card().classes("w-full max-w-2xl mt-4"):
        ui.label("Completed Jobs").classes("text-xl font-bold")
        jobs = list_jobs(status="completed")
        if not jobs:
            ui.label("No completed jobs")
        else:
            for job in jobs:
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"✓ Job #{job['id']} - {job['question'][:50]}...")
                    ui.label(f"{job['findings_count']} findings | ${job['cost']:.2f}")
                    ui.button("View Report", on_click=lambda j=job: ui.navigate.to(f"/job/{j['id']}"))


@ui.page("/job/{job_id}")
async def job_page(job_id: str):
    """Job status and results page"""

    job_status = get_job_status(job_id)

    ui.markdown(f"# Job {job_id}")
    ui.markdown(f"**Question:** {job_status['research_question']}")

    # Status
    with ui.card():
        ui.label(f"Status: {job_status['status']}")
        ui.label(f"Progress: {job_status['iteration']}/{job_status['max_iterations']}")
        ui.label(f"Cost: ${job_status['cost']:.2f}")

        if job_status['status'] == 'running':
            ui.label(f"Avg cost/iteration: ${job_status['cost_per_iter']:.3f}")
            ui.label(f"Estimated total: ${job_status['estimated_total_cost']:.2f}")

    # Recent findings
    if job_status['findings']:
        ui.markdown("## Recent Findings")
        for finding in job_status['findings'][-5:]:
            with ui.card():
                ui.markdown(f"**{finding['title']}**")
                ui.markdown(finding['evidence'])

    # Plots
    if job_status['plots']:
        ui.markdown("## Visualizations")
        for plot_path in job_status['plots'][-5:]:
            ui.image(plot_path)

    # Auto-refresh if running
    if job_status['status'] == 'running':
        ui.timer(3.0, lambda: ui.navigate.reload())


if __name__ == "__main__":
    ui.run(
        title="SHANDY - Autonomous Discovery Agent",
        port=PORT,
        storage_secret=STORAGE_SECRET
    )
```

---

## Project Structure

```
shandy/                          # Renamed from ad-swarm
├── app.py                       # NiceGUI web interface
├── Dockerfile                   # Single container for entire system
├── docker-compose.yml
├── Makefile                     # Deploy, start, stop, logs
├── pyproject.toml               # uv package management
├── uv.lock
├── .env.example
├── .env                         # NOT committed (secrets)
├── .gitignore
├── README.md
│
├── .claude/
│   └── skills/
│       ├── workflow/            # Domain-agnostic discovery workflows
│       │   ├── hypothesis-generation/
│       │   ├── result-interpretation/
│       │   ├── prioritization/
│       │   └── stopping-criteria/
│       └── domain/              # Domain-specific knowledge
│           ├── metabolomics/
│           ├── genomics/
│           └── data-science/
│
├── src/
│   └── ad_swarm/                # Keep Python package name
│       ├── __init__.py
│       ├── orchestrator.py      # Main discovery loop
│       ├── job_manager.py       # Background job queue
│       ├── code_executor.py     # Sandboxed Python execution
│       ├── knowledge_graph.py   # JSON state management
│       ├── literature.py        # PubMed API client
│       ├── cost_tracker.py      # CBORG spend monitoring
│       ├── prompts.py           # Prompt templates
│       └── skills.py            # Skill loading and execution
│
├── jobs/                        # Job storage (mounted volume)
│   ├── job_001/
│   │   ├── config.json
│   │   ├── data/                # Uploaded data
│   │   │   └── Brain_Polar.csv
│   │   ├── knowledge_graph.json # Updated each iteration
│   │   ├── plots/               # Generated visualizations
│   │   │   ├── iter5_volcano.png
│   │   │   └── iter8_pathway.png
│   │   ├── analysis_log.py      # All executed code
│   │   └── final_report.md      # Generated at completion
│   └── job_002/
│
├── static/                      # Static assets for UI
│   └── logo.png
│
├── logs/                        # Application logs
│   └── app_20251113.log
│
├── tests/
│   ├── test_orchestrator.py
│   ├── test_code_executor.py
│   └── test_knowledge_graph.py
│
├── docs/
│   ├── plans/
│   │   └── 2025-11-13-shandy-autonomous-loop-design.md  # This document
│   └── user_guide.md
│
└── notes/                       # Keep existing notes
    ├── data/
    ├── background_reading/
    └── *.md
```

---

## .env Configuration

```bash
# CBORG API Configuration
ANTHROPIC_AUTH_TOKEN=sk-ant-...
ANTHROPIC_BASE_URL=https://api.cborg.lbl.gov
ANTHROPIC_MODEL=anthropic/claude-sonnet

# Application Configuration
PORT=8602
STORAGE_SECRET=change-this-to-random-string-in-production

# Cost Limits
MAX_JOB_COST_USD=10.00           # Stop if single job exceeds
APP_MAX_BUDGET_USD=1000.00       # Total spending limit
WARN_COST_THRESHOLD_USD=5.00     # Warn user at this threshold

# Skills Configuration
SKILLS_ENABLED=true              # Global default for using skills
ALLOW_SKILLS_TOGGLE=true         # Let users override per-job

# Optional: Authentication (like agent-alz-assistant)
DISABLE_AUTH=false
APP_PASSWORD_HASH=...bcrypt_hash...

# Data paths (if not using Docker volumes)
DATA_DIR=/data
JOBS_DIR=/jobs
```

---

## Deployment

**Using Make (like agent-alz-assistant):**

```makefile
# Makefile
.PHONY: start stop restart logs deploy

PORT := $(shell grep '^PORT=' .env 2>/dev/null | cut -d '=' -f 2)

start:
	docker-compose up -d
	@echo "SHANDY running at http://localhost:$(PORT)"

stop:
	docker-compose down

restart: stop start

logs:
	docker-compose logs -f

deploy:
	# Similar to agent-alz-assistant deployment
	@echo "Deploying to production server..."
	# ... deployment steps
```

**Docker Compose:**

```yaml
version: '3.8'

services:
  shandy:
    build: .
    ports:
      - "${PORT}:${PORT}"
    volumes:
      - ./data:/data              # User uploads data here
      - ./jobs:/jobs              # Job results stored here
    environment:
      - ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN}
      - PORT=${PORT}
      - MAX_JOB_COST_USD=${MAX_JOB_COST_USD}
    mem_limit: 4g
    cpus: 2
    restart: unless-stopped
```

---

## Validation Strategy

**Goal:** Validate agent's autonomous discovery capabilities on real scientific datasets

**Success criteria:**
1. Run SHANDY on metabolomics datasets with known biological phenomena
2. Agent autonomously discovers mechanistic insights
3. Domain experts review final report and validate:
   - Are findings biologically plausible?
   - Do they align with known mechanisms?
   - Are statistical methods appropriate?
   - Does it explain the observed phenotype?

**NOT required:**
- Exact reproduction of specific published results
- Matching a predetermined analytical path
- Discovering a specific number of findings

**Bonus points:**
- Agent discovers novel insights not in existing literature
- Agent identifies knowledge gaps
- Agent proposes follow-up experiments

---

## Future Enhancements

### 1. Swarm Architecture
Current design is **swarm-ready**:
- Tools are modular (can be partitioned to specialized agents)
- Knowledge graph is shared state
- Easy migration: Orchestrator spawns multiple agent processes

**Future swarm:**
- **Explorer Agent:** Data exploration, pattern detection
- **Analyst Agent:** Statistical testing, quantification
- **Scholar Agent:** Literature search and synthesis
- **Synthesizer Agent:** Integration, report generation
- **Orchestrator Agent:** Coordinates the others

### 2. LinkML Schema
Define formal schema for knowledge graph:
- Validation of hypothesis/finding structure
- Semantic interoperability with ontologies (GO, KEGG)
- Integration with biomedical knowledge graphs

### 3. Multi-omics Integration
Extend beyond single data type:
- Combine metabolomics + genomics + proteomics
- Cross-domain hypothesis generation
- Integrated pathway analysis

### 4. Interactive Mode
Allow user to intervene during autonomous run:
- Suggest hypotheses to prioritize
- Provide domain expertise
- Steer investigation direction

### 5. Experiment Design
Agent proposes new experiments:
- "To test hypothesis X, we need to measure Y"
- Designs follow-up studies
- Calculates required sample sizes

### 6. MCP Tool Integration
Migrate from direct Python function tools to MCP servers:
- **Current MVP:** Tools implemented as Python functions (simple, works)
- **Future:** Use MCP servers for tool execution (more modular, reusable)
- **Benefits:** Tools become shareable across projects, easier to extend
- **Migration:** Swap tool implementation without changing orchestrator logic
- **Candidate tools:** PubMed search, data visualization, pathway databases

---

## Open Questions

1. **Prompt engineering:** How detailed should prompts be for each iteration?
2. **Hypothesis prioritization:** What scoring formula works best?
3. **Early stopping:** Can agent reliably detect when investigation is complete?
4. **Skill versioning:** How to update skills without breaking running jobs?
5. **Multi-user:** How to handle concurrent jobs with shared CBORG budget?

---

## Next Steps

1. ✅ **Design complete** (this document)
2. ⬜ Set up project structure (`shandy/` repository)
3. ⬜ Implement core orchestrator loop
4. ⬜ Implement code executor with sandboxing
5. ⬜ Create minimal workflow skills
6. ⬜ Create metabolomics domain skills
7. ⬜ Build NiceGUI interface
8. ⬜ Test on metabolomics datasets
9. ⬜ Validate with domain experts
10. ⬜ Iterate and improve

---

## References

- **agent-alz-assistant:** Reference implementation for NiceGUI + deployment
- **Claude Skills:** https://github.com/anthropics/skills
- **PubMed E-utilities API:** https://www.ncbi.nlm.nih.gov/books/NBK25501/

---

**End of Design Document**
