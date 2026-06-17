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

# Budgeting the report prompt against the model's context window. We reserve
# tokens for the system prompt, tool definitions, the model's own output, and
# the non-literature sections of the report prompt, then spend the rest on
# literature abstracts. CHARS_PER_TOKEN is a conservative chars->tokens factor
# (dense scientific text), so the char budget under-fills rather than overflows.
_REPORT_PROMPT_RESERVE_TOKENS = 12000
_REPORT_PROMPT_CHARS_PER_TOKEN = 3.5
_REPORT_PROMPT_MIN_ABSTRACT_CHARS = 2000


def _report_abstract_budget_chars(context_window_tokens: int) -> int:
    """Char budget for literature abstracts, derived from the model's context.

    Pure function of the resolved window: the caller passes the agent's cached
    ``model_profile.context_window_tokens`` so this builder does no I/O.
    """
    spare_tokens = context_window_tokens - _REPORT_PROMPT_RESERVE_TOKENS
    return max(
        _REPORT_PROMPT_MIN_ABSTRACT_CHARS,
        int(spare_tokens * _REPORT_PROMPT_CHARS_PER_TOKEN),
    )


def _format_job_description_section(description: str | None) -> str:
    """Return optional user-provided job context for prompts."""
    if not description:
        return ""
    return f"""Additional job context provided by the scientist:

{description}
"""


class FeedbackWaitResult(TypedDict):
    """Outcome for co-investigate feedback waiting."""

    outcome: Literal["feedback", "timeout", "continued", "cancelled"]
    feedback_text: str | None


def build_initial_prompt(
    research_question: str,
    max_iterations: int,
    data_files: list[str],
    ks: KnowledgeState,
    description: str | None = None,
) -> str:
    """Build the prompt for iteration 1."""
    description_context = _format_job_description_section(description)
    if data_files:
        data_context = (
            f"Data summary:\n"
            f"- Files: {data_files}\n"
            f"- Columns: {ks.data['data_summary'].get('columns', [])}\n"
            f"- Samples: {ks.data['data_summary'].get('n_samples', 'Unknown')}\n"
            f"- Accessing data in execute_code: the primary file is pre-loaded "
            f"as the `data` DataFrame, and every file is listed in `data_files` "
            f"with a ready-to-read `path`. Do not construct file paths yourself "
            f"or reuse paths from the shell. They do not exist in the code "
            f"executor."
        )
    else:
        data_context = (
            "No data files provided. You may use literature search and computational methods."
        )

    return f"""Begin autonomous discovery for this research question:

{research_question}
{description_context}

You are on iteration 1 of {max_iterations}.

{data_context}

Tools (MCP): `execute_code` (analysis, stats, plots), `search_pubmed` (literature),
`update_knowledge_state` (record a finding with its evidence), `save_iteration_summary`
(end-of-iteration recap), `set_status` (optional progress label).

An iteration is not complete until you run analysis and record it:
1. Plan briefly, then call `execute_code` and/or `search_pubmed` this turn.
2. Record findings with `update_knowledge_state`.
3. Finish with `save_iteration_summary` (1-2 sentences).

`set_status` is only a label, not progress. Do not end your turn after only calling it:
if you announce an action, perform it with a tool before the turn ends.

Start now.
"""


def build_iteration_prompt(
    iteration: int,
    max_iterations: int,
    ks: KnowledgeState,
    pending_feedback: str | None = None,
    description: str | None = None,
) -> str:
    """Build the prompt for iterations 2-N."""
    description_context = _format_job_description_section(description)
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
{feedback_section}
{description_context}
{ks.get_summary()}

---

An iteration is not complete until you run analysis and record it:
1. Call `execute_code` and/or `search_pubmed` this turn to make real progress.
2. Record findings with `update_knowledge_state`.
3. Finish with `save_iteration_summary` (1-2 sentences).

`set_status` is only a label, not progress, and a status-only turn wastes the iteration.
If you announce an action (for example "Performing pathway enrichment"), perform it with a
tool before the turn ends, not just describe it."""


def build_report_prompt(
    research_question: str,
    ks: KnowledgeState,
    *,
    job_dir: Path | None = None,
    description: str | None = None,
    file_write_tool: str = "Write",
    context_window_tokens: int,
) -> str:
    """Build the prompt for the final report generation iteration.

    The prompt must be explicit that the agent should write the FULL report
    content (not a summary or table of contents) and must name the exact
    file-writing tool so the model invokes it rather than printing the content.

    Args:
        research_question: The research question that drives the agent.
        ks: KnowledgeState for the job, providing the report outline.
        job_dir: Absolute path to the job directory.  When provided the prompt
            tells the agent the exact file path to write (the write tool
            requires an absolute path).
        description: Optional user-provided job context.
        file_write_tool: The backend's file-writing tool name (e.g.
            ``apply_patch`` for codex, ``Write`` for Claude). Named verbatim in
            the prompt so the model calls that tool instead of guessing.
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

{_format_job_description_section(description)}

{ks.get_report_outline(abstract_budget_chars=_report_abstract_budget_chars(context_window_tokens))}

{figure_section}

---

## Instructions

**CRITICAL (file path):** Write the report to exactly this path:
`{report_path}`
Do NOT write to `/tmp/`, `~/`, or any other location, or the system will not find it.

**CRITICAL (action):** Call the `{file_write_tool}` tool to actually CREATE this file on disk.
Do not just describe the report or print it in your reply, and do not emit the tool call
as text. Invoke `{file_write_tool}` so the file exists at the path above when you are done.

**CRITICAL (content):** The file must contain the COMPLETE, FULL text of every
section, not a table of contents, not a summary of sections, not a pointer to
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

4. **Embedding figures (required):**
   - You MUST embed every available figure that supports a finding, using this syntax: `{{{{figure:filename.png|caption=Your caption here}}}}`
   - Include a figure for each major finding that has one. A report that omits available figures is incomplete
   - Place figures near the text that discusses them
   - Only reference figures listed in the "Available Figures" section above

5. **Citation integrity:**
   - For each finding, use the provided citation snippets as the basis for your references — do not re-derive which papers support which claims from the literature list
   - Only attribute claims to papers based on the abstracts or citation snippets provided in the knowledge outline above — do not infer paper content from titles alone

**Remember:** The content of `{report_path}` IS the deliverable the user receives.
It must be a complete, self-contained document, not a summary or index. Writing this
report is the only task for this step. The consensus answer comes as a separate step.
"""


def build_consensus_prompt(research_question: str) -> str:
    """Prompt for the dedicated consensus-answer turn.

    Run as its own single-purpose turn after the report so a weaker model does
    not drop the report by trying to do both at once. The model writes the
    consensus itself.
    """
    return f"""The final report is written. Now record the consensus answer for this
research question:

{research_question}

Call the `set_consensus_answer` tool with a direct 1-3 sentence answer to the research
question. Be direct, no citations, no hedging. Calling that tool is the only action
for this step."""


def build_report_retry_prompt(
    research_question: str,
    ks: KnowledgeState,
    *,
    job_dir: Path | None = None,
    description: str | None = None,
    file_write_tool: str = "Write",
    context_window_tokens: int,
) -> str:
    """Re-ask for the report when the previous turn ended without the file.

    A weak model re-anchors on the *last* instruction it received, so a bare
    "you forgot, write the file" reminder reframes the step as a trivial chore
    and the model answers it literally (a one-line stub, or a description of a
    report instead of the report). The fix is to restate the *entire* task: a
    short forceful correction followed by the full self-contained report spec
    (findings outline, structure, exact path, and the "write it, do not
    describe it" rules).
    """
    if job_dir is not None:
        report_path = str(job_dir.resolve() / "final_report.md")
    else:
        report_path = "./final_report.md"

    correction = f"""Your previous turn did NOT produce the report. Either you never called the
`{file_write_tool}` tool, or you emitted the tool call as text / described the report
instead of writing it to disk.

That output is rejected. For this turn:
- Do NOT narrate, summarize, or explain what the report will contain.
- Do NOT print the report body or the tool call as text in your reply.
- The ONLY acceptable action is to invoke the `{file_write_tool}` tool and write the
  COMPLETE report content to `{report_path}`.

The full task is restated below so nothing is missing. Follow it exactly.

---

"""
    base = build_report_prompt(
        research_question,
        ks,
        job_dir=job_dir,
        description=description,
        file_write_tool=file_write_tool,
        context_window_tokens=context_window_tokens,
    )
    return correction + base


def build_consensus_retry_prompt(research_question: str) -> str:
    """Focused re-ask when the consensus turn did not record an answer."""
    return f"""You did NOT record the consensus answer. Call the `set_consensus_answer`
tool now with a direct 1-3 sentence answer to the research question:

{research_question}

Calling that tool is the only action for this step."""


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
            job_title = job.short_title or job.research_question
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
