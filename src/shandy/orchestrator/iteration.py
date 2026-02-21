"""
Iteration helpers for the SHANDY discovery loop.

Prompt construction, iteration counter management, and status updates.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shandy.knowledge_state import KnowledgeState

logger = logging.getLogger(__name__)

FEEDBACK_TIMEOUT_SECONDS = 15 * 60  # 15 minutes


def build_initial_prompt(
    research_question: str,
    max_iterations: int,
    data_files: list[str],
    ks: KnowledgeState,
) -> str:
    """Build the prompt for iteration 1."""
    if data_files:
        data_context = (
            f"Data summary:\n"
            f"- Files: {data_files}\n"
            f"- Columns: {ks.data['data_summary'].get('columns', [])}\n"
            f"- Samples: {ks.data['data_summary'].get('n_samples', 'Unknown')}"
        )
    else:
        data_context = (
            "No data files provided. You may use literature search and computational methods."
        )

    return f"""Begin autonomous discovery for this research question:

{research_question}

You will run for a maximum of {max_iterations} iterations.

{data_context}

You have access to MCP tools for analysis, literature search, and recording findings.
Examples include (there may be others - explore what's available):
- execute_code: Analyze data, run statistical tests, create visualizations
- search_pubmed: Search for relevant papers
- update_knowledge_state: Record confirmed findings with statistical evidence
- save_iteration_summary: Record a summary of what you investigated and learned
- set_status: Update your status to let users know what you're working on

**REQUIRED: Your very first tool call MUST be set_status** (e.g., "Planning investigation strategy").
After that, call set_status before every significant action so users can follow your progress.
At the end of each iteration, call save_iteration_summary with a 1-2 sentence
plain-language summary of what you investigated and what you learned.

Start now.
"""


def build_iteration_prompt(
    iteration: int,
    max_iterations: int,
    ks: KnowledgeState,
    pending_feedback: str | None = None,
) -> str:
    """Build the prompt for iterations 2-N."""
    feedback_section = ""
    if pending_feedback:
        feedback_section = f"""
## Scientist Feedback
The scientist has provided the following guidance after reviewing your previous iteration:
> {pending_feedback}

Continue your investigation, taking this guidance into account. Use your judgment to balance
the scientist's suggestions with your own analysis of what will be most productive.

---
"""
    return f"""# Iteration {iteration}/{max_iterations}
{feedback_section}
{ks.get_summary()}

---

**REQUIRED: Call set_status immediately** before doing anything else in this iteration.
Then continue your investigation using the available MCP tools.
Examples: execute_code, search_pubmed, update_knowledge_state, save_iteration_summary, set_status.
Think step by step about what will provide the most insight, then actively use the tools to execute your investigation.

Call set_status before every significant action so users can follow your progress.
At the end of this iteration, call save_iteration_summary with a brief summary of what you investigated and learned."""


def build_report_prompt(research_question: str, ks: KnowledgeState) -> str:
    """Build the prompt for the final report generation iteration.

    The agent starts a fresh session, so all context comes from the summary
    below and the files on disk.  The prompt must be explicit that the agent
    should write the FULL report content — not a summary or table of contents.
    """
    return f"""All iterations are complete. Write the final report for this research question:

{research_question}

{ks.get_report_outline()}

---

## Instructions

**CRITICAL:** You must write the COMPLETE report directly into `final_report.md`.
The file must contain the FULL text of every section — not a table of contents,
not a summary of sections, not a pointer to another file.  If `final_report.md`
already exists, overwrite it entirely.

Read `knowledge_state.json` for the full data (findings, hypotheses, literature,
iteration summaries) and incorporate it into the report.

1. **Write the full report** to `final_report.md` in the current directory.
   The report should be comprehensive and detailed — typically 2,000+ words for
   a multi-iteration investigation.  Every section must contain its actual content.

2. **Report structure:**
   - **Executive Summary** (2-3 paragraphs) — key takeaways for busy readers
   - **Key Findings** — each finding with its statistical evidence, expanded into
     full prose paragraphs (not just bullet points from the knowledge state)
   - **Mechanistic Model/Interpretation** — synthesize findings into a coherent
     narrative; use ASCII diagrams or tables where helpful
   - **Evidence Base** — key literature with PMID links and how each paper
     supports or challenges your findings
   - **Limitations and Knowledge Gaps**
   - **Proposed Follow-up Experiments/Actions** — concrete, actionable next steps

3. **Formatting:**
   - Use markdown tables for comparative data and study results
   - Include PMID links as `[PMID: 12345678](https://pubmed.ncbi.nlm.nih.gov/12345678/)`
   - Use proper heading hierarchy (h2 for sections, h3 for subsections)
   - Use **bold** for key terms, *italic* for paper titles
   - Lead with the answer, then provide evidence (inverted pyramid)
   - Quantify findings (e.g., "3 of 5 studies found...")
   - Acknowledge limitations and uncertainty clearly

4. **After writing the report**, call `set_consensus_answer` with a direct 1-3 sentence
   answer to the research question.  Be direct — no citations or hedging.

**Remember:** The content of `final_report.md` IS the deliverable the user receives.
It must be a complete, self-contained document — not a summary or index.
"""


def increment_ks_iteration(ks_path: Path) -> None:
    """
    Safely increment the knowledge graph iteration counter.

    Uses atomic write (temp file + rename) so a crash mid-write never
    corrupts the knowledge state file.
    """
    ks = KnowledgeState.load(ks_path)
    ks.data["iteration"] += 1
    ks.save(ks_path)


def update_job_status(job_dir: Path, status: str) -> None:
    """Update job status in config.json and send ntfy notification if applicable."""
    config_path = job_dir / "config.json"
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    old_status = config.get("status")
    config["status"] = status

    if status == "awaiting_feedback":
        config["awaiting_feedback_since"] = datetime.now(timezone.utc).isoformat()
    elif "awaiting_feedback_since" in config:
        del config["awaiting_feedback_since"]

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    if status == "awaiting_feedback" and old_status != "awaiting_feedback":
        _send_iteration_notification(job_dir, config)


def _send_iteration_notification(job_dir: Path, config: dict[str, Any]) -> None:
    """Send ntfy notification when an iteration completes."""
    if not config.get("ntfy_enabled") or not config.get("ntfy_topic"):
        return

    try:
        import httpx

        from shandy.settings import get_settings

        settings = get_settings()
        topic = config["ntfy_topic"]
        job_id = config["job_id"]

        short_title = config.get("short_title")
        if not short_title:
            research_q = config.get("research_question", "Unknown job")
            short_title = research_q[:50] + "..." if len(research_q) > 50 else research_q

        ks_path = job_dir / "knowledge_state.json"
        iteration = 1
        if ks_path.exists():
            ks = KnowledgeState.load(ks_path)
            iteration = ks.data.get("iteration", 1)

        url = f"https://ntfy.sh/{topic}"
        headers = {
            "Title": f"Iteration {iteration} Complete",
            "Priority": "default",
            "Tags": "white_check_mark",
            "Click": f"{settings.base_url}/job/{job_id}",
        }
        message = f"'{short_title}' has completed iteration {iteration}."

        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, content=message, headers=headers)
            response.raise_for_status()
            logger.info("Sent iteration notification to topic %s", topic)
    except Exception as e:
        logger.warning("Failed to send ntfy notification: %s", e)


def wait_for_feedback_or_timeout(
    job_dir: Path, timeout_seconds: int = FEEDBACK_TIMEOUT_SECONDS
) -> str | None:
    """
    Wait for scientist feedback or timeout (coinvestigate mode).

    Returns feedback text if submitted, None if timeout or cancelled.
    """
    config_path = job_dir / "config.json"
    ks_path = job_dir / "knowledge_state.json"

    start_time = time.time()

    ks = KnowledgeState.load(ks_path)
    current_iteration = ks.data["iteration"]
    last_feedback_count = len(ks.data.get("feedback_history", []))

    logger.info("Waiting for scientist feedback (timeout: %ds)", timeout_seconds)

    while True:
        elapsed = time.time() - start_time

        if elapsed >= timeout_seconds:
            logger.info("Feedback timeout after %.0fs - auto-continuing", elapsed)
            return None

        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        if config.get("status") == "cancelled":
            logger.info("Job cancelled while waiting for feedback")
            return None

        ks = KnowledgeState.load(ks_path)
        feedback_history = ks.data.get("feedback_history", [])

        if len(feedback_history) > last_feedback_count:
            latest = feedback_history[-1]
            if latest.get("after_iteration") == current_iteration:
                logger.info("Received feedback: %s...", latest["text"][:100])
                return str(latest["text"])

        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        if config.get("status") == "running":
            logger.info("Continue signal received (no feedback)")
            return None

        time.sleep(2)
