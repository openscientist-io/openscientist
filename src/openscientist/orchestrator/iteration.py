"""
Iteration helpers for the OpenScientist discovery loop.

Prompt construction, iteration counter management, and status updates.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection
from pathlib import Path
from typing import Literal, TypedDict
from uuid import UUID

from sqlalchemy import func, select, update

from openscientist.database.models.job import Job as JobModel
from openscientist.database.retry import CONTAINER_DB_RETRY_SECONDS, retry_async
from openscientist.database.session import SessionOpener, async_session_local
from openscientist.knowledge_state import KnowledgeState
from openscientist.prompts.common import namespace_tool_mentions

logger = logging.getLogger(__name__)

FEEDBACK_TIMEOUT_SECONDS = 15 * 60  # 15 minutes

#: Statuses after which a job holds no slot, so the scheduler stops counting it.
_ENDED_STATUSES = frozenset({"completed", "failed", "cancelled"})
#: Statuses that end the agent's current work, so its status label goes with it.
_LABEL_ENDING_STATUSES = _ENDED_STATUSES | {"awaiting_feedback"}

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
    *,
    tool_prefix: str,
) -> str:
    """Build the prompt for iteration 1.

    ``tool_prefix`` namespaces the MCP tool names in the instructions this
    module writes. It is applied before the scientist's question and
    description are interpolated, so their words reach the model as written.
    """
    description_context = _format_job_description_section(description)
    if data_files:
        access_note = namespace_tool_mentions(
            "- Accessing data in `execute_code`: the primary file is pre-loaded "
            "as the `data` DataFrame, and every file is listed in `data_files` "
            "with a ready-to-read `path`. Do not construct file paths yourself "
            "or reuse paths from the shell. They do not exist in the code "
            "executor.",
            tool_prefix,
        )
        data_context = (
            f"Data summary:\n"
            f"- Files: {data_files}\n"
            f"- Columns: {ks.data['data_summary'].get('columns', [])}\n"
            f"- Samples: {ks.data['data_summary'].get('n_samples', 'Unknown')}\n"
            f"{access_note}"
        )
    else:
        data_context = (
            "No data files provided. You may use literature search and computational methods."
        )

    instructions = namespace_tool_mentions(
        """Tools (MCP): `execute_code` (analysis, stats, plots), `search_pubmed` (literature),
`update_knowledge_state` (record a finding with its evidence), `save_iteration_summary`
(end-of-iteration recap), `set_status` (optional progress label).

An iteration is not complete until you run analysis and record it:
1. Plan briefly, then call `execute_code` and/or `search_pubmed` this turn.
2. Record findings with `update_knowledge_state`.
3. Finish with `save_iteration_summary` (1-2 sentences).

`set_status` is only a label, not progress. Do not end your turn after only calling it:
if you announce an action, perform it with a tool before the turn ends.

Start now.""",
        tool_prefix,
    )

    return f"""Begin autonomous discovery for this research question:

{research_question}
{description_context}

You are on iteration 1 of {max_iterations}.

{data_context}

{instructions}
"""


def build_iteration_prompt(
    iteration: int,
    max_iterations: int,
    ks: KnowledgeState,
    pending_feedback: str | None = None,
    description: str | None = None,
    *,
    tool_prefix: str,
) -> str:
    """Build the prompt for iterations 2-N.

    ``tool_prefix`` namespaces the MCP tool names in these instructions only,
    leaving the scientist's feedback and description as written.
    """
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
    instructions = namespace_tool_mentions(
        """An iteration is not complete until you run analysis and record it:
1. Call `execute_code` and/or `search_pubmed` this turn to make real progress.
2. Record findings with `update_knowledge_state`.
3. Finish with `save_iteration_summary` (1-2 sentences).

`set_status` is only a label, not progress, and a status-only turn wastes the iteration.
If you announce an action (for example "Performing pathway enrichment"), perform it with a
tool before the turn ends, not just describe it.""",
        tool_prefix,
    )
    return f"""# Iteration {iteration} of {max_iterations}
{feedback_section}
{description_context}
{ks.get_summary()}

---

{instructions}"""


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


def build_consensus_prompt(research_question: str, *, tool_prefix: str) -> str:
    """Prompt for the dedicated consensus-answer turn.

    Run as its own single-purpose turn after the report so a weaker model does
    not drop the report by trying to do both at once. The model writes the
    consensus itself.
    """
    instruction = namespace_tool_mentions(
        """Call the `set_consensus_answer` tool with a direct 1-3 sentence answer to the research
question. Be direct, no citations, no hedging. Calling that tool is the only action
for this step.""",
        tool_prefix,
    )
    return f"""The final report is written. Now record the consensus answer for this
research question:

{research_question}

{instruction}"""


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


def build_consensus_retry_prompt(research_question: str, *, tool_prefix: str) -> str:
    """Focused re-ask when the consensus turn did not record an answer."""
    opening = namespace_tool_mentions(
        "You did NOT record the consensus answer. Call the `set_consensus_answer`\n"
        "tool now with a direct 1-3 sentence answer to the research question:",
        tool_prefix,
    )
    return f"""{opening}

{research_question}

Calling that tool is the only action for this step."""


async def increment_ks_iteration(job_id: str) -> None:
    """Increment the persisted knowledge-state iteration counter."""
    ks = await KnowledgeState.load_from_database(job_id)
    ks.data["iteration"] += 1
    await ks.save_to_database(job_id)


async def _get_job_status(job_id: str) -> str | None:
    """Fetch current job status from the database, retrying a lost connection
    for :data:`CONTAINER_DB_RETRY_SECONDS`."""

    async def read() -> str | None:
        async with async_session_local() as session:
            result = await session.execute(
                select(JobModel.status).where(JobModel.id == UUID(job_id))
            )
            return result.scalar_one_or_none()

    return await retry_async(
        read, within=CONTAINER_DB_RETRY_SECONDS, what=f"reading the status of job {job_id}"
    )


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
    allowed_from: Collection[str],
    open_session: SessionOpener,
    retry_for: float,
) -> bool:
    values: dict[str, object] = {"status": status}
    if error_message:
        values["error_message"] = error_message
    if status in _ENDED_STATUSES:
        values["ended_at"] = func.now()
    if status in _LABEL_ENDING_STATUSES:
        values["agent_status"] = None
        values["agent_status_updated_at"] = None
    stmt = (
        update(JobModel)
        .where(JobModel.id == job_uuid, JobModel.status.in_(list(allowed_from)))
        .values(**values)
        .returning(JobModel.id)
    )

    async def write() -> bool:
        async with open_session() as session:
            if (await session.execute(stmt)).scalar_one_or_none() is None:
                logger.info(
                    "Job %s is not in %s, so it stays as it is", job_id, sorted(allowed_from)
                )
                return False
            await session.commit()
            return True

    return await retry_async(write, within=retry_for, what=f"moving job {job_id} to {status}")


async def update_job_status(
    job_dir: Path,
    status: str,
    error_message: str | None = None,
    *,
    allowed_from: Collection[str],
    open_session: SessionOpener | None = None,
    retry_for: float = CONTAINER_DB_RETRY_SECONDS,
) -> bool:
    """Move the job to ``status`` if it is in one of ``allowed_from``, in
    ``open_session``, else the process's own login. Returns whether it applied.

    A lost connection is retried for ``retry_for`` seconds and then raised, as
    is any other database error."""
    job_id = job_dir.name

    job_uuid = _parse_job_uuid(job_id)
    if job_uuid is None:
        return False

    return await _persist_job_status(
        job_id,
        job_uuid,
        status,
        error_message,
        allowed_from,
        open_session or async_session_local,
        retry_for,
    )


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
