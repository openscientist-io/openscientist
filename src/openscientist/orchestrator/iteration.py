"""
Iteration helpers for the OpenScientist discovery loop.

Prompt construction, iteration counter management, and status updates.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Literal, TypedDict
from uuid import UUID

from sqlalchemy import select

from openscientist.database.models import User
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import AsyncSessionLocal
from openscientist.job.types import JobStatus
from openscientist.knowledge_state import KnowledgeState
from openscientist.ntfy import notify_job_status_change

logger = logging.getLogger(__name__)

FEEDBACK_TIMEOUT_SECONDS = 15 * 60  # 15 minutes


class FeedbackWaitResult(TypedDict):
    """Outcome for co-investigate feedback waiting."""

    outcome: Literal["feedback", "timeout", "continued", "cancelled"]
    feedback_text: str | None


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

**You are now on iteration 1 of {max_iterations}.** When writing summaries, always refer to this as "Iteration 1".

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
    return f"""# Iteration {iteration} of {max_iterations}

**You are now on iteration {iteration}.** When writing summaries, always refer to this as "Iteration {iteration}".
{feedback_section}
{ks.get_summary()}

---

**REQUIRED: Call set_status immediately** before doing anything else in this iteration.
Then continue your investigation using the available MCP tools.
Examples: execute_code, search_pubmed, update_knowledge_state, save_iteration_summary, set_status.
Think step by step about what will provide the most insight, then actively use the tools to execute your investigation.

Call set_status before every significant action so users can follow your progress.
At the end of this iteration, call save_iteration_summary with a brief summary of what you investigated and learned."""


def build_report_prompt(
    research_question: str,
    ks: KnowledgeState,
    *,
    job_dir: Path | None = None,
) -> str:
    """Build the prompt for the final report generation iteration.

    The agent starts a fresh session, so all context comes from the summary
    below and the files on disk.  The prompt must be explicit that the agent
    should write the FULL report content — not a summary or table of contents.

    Args:
        research_question: The original research question.
        ks: Current knowledge state with findings and summaries.
        job_dir: Absolute path to the job directory.  When provided the prompt
            tells the agent the exact file path to write (the Write tool
            requires an absolute path).
    """
    if job_dir is not None:
        report_path = str(job_dir.resolve() / "final_report.md")
    else:
        report_path = "./final_report.md"

    # Build figure inventory section if plots are available
    figure_section = ""
    if job_dir is not None:
        from openscientist.report.figures import (
            build_figure_inventory,
            format_figure_inventory_prompt,
        )

        cards = build_figure_inventory(job_dir)
        figure_section = format_figure_inventory_prompt(cards)

    return f"""All iterations are complete. Write the final report for this research question:

{research_question}

{ks.get_report_outline()}

{figure_section}

---

## Instructions

**CRITICAL — file path:** Write the report to exactly this path:
`{report_path}`
Do NOT write to `/tmp/`, `~/`, or any other location — the system will not find it.

**CRITICAL — content:** The file must contain the COMPLETE, FULL text of every
section — not a table of contents, not a summary of sections, not a pointer to
another file.  If `final_report.md` already exists, overwrite it entirely.

Use the provided knowledge summary below for findings, hypotheses, literature,
and iteration summaries.

1. **Write the full report** to `{report_path}`.
   The report should be comprehensive and detailed — typically 2,000+ words for
   a multi-iteration investigation.  Every section must contain its actual content.

2. **Report structure:**
   - **Summary** (2-3 paragraphs) — key takeaways for busy readers
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

4. **Embedding figures:**
   - To include a plot in the report, use this syntax: `{{{{figure:filename.png|caption=Your caption here}}}}`
   - Select the most informative plots — aim for ~1 figure per major finding
   - Place figures near the text that discusses them
   - Only reference figures listed in the "Available Figures" section above

5. **Citation integrity:**
   - For each finding, use the provided citation snippets as the basis for your references — do not re-derive which papers support which claims from the literature list
   - Only attribute claims to papers based on the abstracts or citation snippets provided in the knowledge outline above — do not infer paper content from titles alone

6. **After writing the report**, call `set_consensus_answer` with a direct 1-3 sentence
   answer to the research question.  Be direct — no citations or hedging.

**Remember:** The content of `{report_path}` IS the deliverable the user receives.
It must be a complete, self-contained document — not a summary or index.
"""


def build_review_prompt(
    research_question: str,
    *,
    job_dir: Path,
    final_report_markdown: str,
    job_context: str = "",
) -> str:
    """Build the prompt for an independent review of a completed analysis.

    The reviewer agent returns Markdown; the application persists that output
    as ``reviewer_report.md`` after the executor completes.
    """
    context_section = job_context.strip() or "No structured job context was available."

    return f"""A completed OpenScientist analysis is ready for independent scientific review.

Research question:
{research_question}

Working directory:
{job_dir.resolve()}

The current directory contains the completed job bundle. Inspect the final report and, when needed,
supporting artifacts such as provenance transcripts, analysis logs, data summaries, and uploaded data
files. Do not modify any files. The application will save your Markdown response as
`reviewer_report.md`.

## Structured Job Context

{context_section}

## Final Report To Review

<final_report_markdown>
{final_report_markdown}
</final_report_markdown>

## Review Instructions

Act as an external scientific reviewer evaluating the rigor of the completed analysis. Be critical,
specific, and calibrated. Prefer concrete evidence from the report or artifacts over generic advice.
If you cannot verify a concern from available artifacts, say so explicitly.

Your response must be a self-contained Markdown review with these sections:

1. Summary
2. Strengths
3. Major Concerns
4. Minor Concerns
5. Recommendations
6. Review Limitations

Focus especially on:

- Whether the analysis answers the research question
- Statistical validity, sample handling, preprocessing, normalization, and multiple-testing control
- Whether claims are supported by measured data and cited literature
- Missing controls, omitted data files, unsupported causal or translational claims
- Reproducibility problems in code, provenance, artifacts, or reported methods
- Concrete next steps that would make the analysis more publishable

Do not rewrite the original report. Do not produce a generic checklist. Return only the Markdown
review.
"""


def increment_ks_iteration(job_id: str) -> None:
    """Increment the persisted knowledge-state iteration counter."""
    ks = KnowledgeState.load_from_database_sync(job_id)
    ks.data["iteration"] += 1
    ks.save_to_database_sync(job_id)


async def _get_job_status(job_id: str) -> str | None:
    """Fetch current job status from the database."""
    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            result = await session.execute(
                select(JobModel.status).where(JobModel.id == UUID(job_id))
            )
            return result.scalar_one_or_none()
    except Exception as e:
        logger.warning("Failed to fetch status for job %s: %s", job_id, e)
        return None


def _parse_job_uuid(job_id: str) -> UUID | None:
    try:
        return UUID(job_id)
    except ValueError:
        logger.warning("Cannot update status for invalid job id: %s", job_id)
        return None


async def _persist_job_status(
    job_id: str,
    job_uuid: UUID,
    status: str,
    error_message: str | None,
) -> tuple[str | None, UUID | None, str | None, int, bool, str | None] | None:
    old_status: str | None = None
    owner_id: UUID | None = None
    job_title: str | None = None
    current_iteration = 1
    ntfy_enabled = False
    ntfy_topic: str | None = None

    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            job_result = await session.execute(select(JobModel).where(JobModel.id == job_uuid))
            job = job_result.scalar_one_or_none()
            if job is None:
                logger.warning("Cannot update status for missing job %s", job_id)
                return None

            old_status = job.status
            if old_status != status:
                job.status = status
                await session.flush()
            if error_message:
                job.error_message = error_message

            owner_id = job.owner_id
            job_title = job.short_title or job.title
            try:
                current_iteration = int(job.current_iteration)
                if current_iteration < 1:
                    current_iteration = 1
            except (TypeError, ValueError):
                current_iteration = 1

            if owner_id is not None:
                user_result = await session.execute(
                    select(User.ntfy_enabled, User.ntfy_topic).where(User.id == owner_id)
                )
                user_row = user_result.first()
                if user_row:
                    ntfy_enabled = bool(user_row.ntfy_enabled)
                    ntfy_topic = user_row.ntfy_topic

            await session.commit()
    except Exception as e:
        logger.warning("Failed to update status for job %s: %s", job_id, e)
        return None

    return old_status, owner_id, job_title, current_iteration, ntfy_enabled, ntfy_topic


def _should_notify_awaiting_feedback(
    *,
    status: str,
    old_status: str | None,
    owner_id: UUID | None,
    ntfy_enabled: bool,
    job_title: str | None,
) -> bool:
    if status != JobStatus.AWAITING_FEEDBACK or old_status == JobStatus.AWAITING_FEEDBACK:
        return False
    return bool(owner_id and ntfy_enabled and job_title)


async def update_job_status(
    job_dir: Path,
    status: str,
    error_message: str | None = None,
) -> None:
    """Update job status in the database and notify on feedback wait transitions."""
    job_id = job_dir.name

    job_uuid = _parse_job_uuid(job_id)
    if job_uuid is None:
        return

    db_result = await _persist_job_status(job_id, job_uuid, status, error_message)
    if db_result is None:
        return

    old_status, owner_id, job_title, current_iteration, ntfy_enabled, ntfy_topic = db_result
    if not _should_notify_awaiting_feedback(
        status=status,
        old_status=old_status,
        owner_id=owner_id,
        ntfy_enabled=ntfy_enabled,
        job_title=job_title,
    ):
        return

    if owner_id is None or not job_title:
        return

    try:
        await notify_job_status_change(
            user_id=owner_id,
            job_id=job_id,
            job_title=job_title,
            new_status="awaiting_feedback",
            iteration=current_iteration,
            ntfy_topic=ntfy_topic,
        )
    except Exception as e:
        logger.warning("Failed to send awaiting_feedback notification for %s: %s", job_id, e)


async def wait_for_feedback_or_timeout(
    job_dir: Path, timeout_seconds: int = FEEDBACK_TIMEOUT_SECONDS
) -> FeedbackWaitResult:
    """
    Wait for scientist feedback or timeout (coinvestigate mode).

    Returns:
        Structured feedback wait outcome:
            - outcome="feedback" with feedback_text when scientist submits feedback
            - outcome="timeout" when timeout elapsed
            - outcome="cancelled" when job is cancelled
            - outcome="continued" when user resumes without feedback
    """
    job_id = job_dir.name

    start_time = time.monotonic()

    ks = await KnowledgeState.load_from_database(job_id)
    current_iteration = ks.data["iteration"]
    last_feedback_count = len(ks.data.get("feedback_history", []))

    logger.info("Waiting for scientist feedback (timeout: %ds)", timeout_seconds)

    while True:
        elapsed = time.monotonic() - start_time

        if elapsed >= timeout_seconds:
            logger.info("Feedback timeout after %.0fs - auto-continuing", elapsed)
            return {"outcome": "timeout", "feedback_text": None}

        status = await _get_job_status(job_id)
        if status == "cancelled":
            logger.info("Job cancelled while waiting for feedback")
            return {"outcome": "cancelled", "feedback_text": None}
        if status == "running":
            logger.info("Continue signal received (no feedback)")
            return {"outcome": "continued", "feedback_text": None}

        ks = await KnowledgeState.load_from_database(job_id)
        feedback_history = ks.data.get("feedback_history", [])

        if len(feedback_history) > last_feedback_count:
            latest = feedback_history[-1]
            if latest.get("after_iteration") == current_iteration:
                logger.info("Received feedback: %s...", latest["text"][:100])
                return {"outcome": "feedback", "feedback_text": str(latest["text"])}

        await asyncio.sleep(2)
