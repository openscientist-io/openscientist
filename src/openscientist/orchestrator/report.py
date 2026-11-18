"""Report-generation phase of a discovery run."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from openscientist.agent.base import AbstractAgent, IterationResult
from openscientist.artifact_packager import TRANSCRIPT_SUFFIX
from openscientist.database.models import CostRecord
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import async_session_local
from openscientist.exceptions import OpenScientistError
from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import (
    build_consensus_prompt,
    build_consensus_retry_prompt,
    build_report_prompt,
    build_report_retry_prompt,
    update_job_status,
)
from openscientist.orchestrator.provenance import _STAMP_DEADLINE_SECONDS
from openscientist.providers.base import Provider
from openscientist.run_environment import run_environment_text
from openscientist.transcript import TranscriptEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ReportOutcome:
    """Outcome of report-generation phase."""

    success: bool
    error: str


def _save_report_transcript(job_dir: Path, transcript: list[TranscriptEntry]) -> None:
    """Persist report-generation transcript artifact."""
    from openscientist.orchestrator.discovery import _save_transcript

    provenance_dir = job_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    _save_transcript(provenance_dir / f"report{TRANSCRIPT_SUFFIX}", transcript)


def _ensure_report_written(
    report_path: Path,
    report_result: IterationResult,
    *,
    baseline_mtime_ns: int | None = None,
) -> bool:
    """Return True only if the report was actually (re)written this turn.

    A weak model can end the turn claiming "report written" without calling its
    file-writing tool, so existence alone is not proof. ``baseline_mtime_ns``
    is the mtime captured before the turn (None if it did not exist), and the
    file counts as written only if it now exists and is strictly newer. If the
    agent wrote the file to a subdirectory, move it to the expected path.
    Returns False when no fresh report can be found, so the caller re-asks,
    then marks the job as failed.
    """

    def _is_fresh(path: Path) -> bool:
        if baseline_mtime_ns is None:
            return True
        try:
            return path.stat().st_mtime_ns > baseline_mtime_ns
        except OSError:
            return False

    if report_path.exists() and _is_fresh(report_path):
        return True

    # A nested report the agent just produced is fresh by construction.
    job_dir = report_path.parent
    for found in job_dir.rglob("final_report.md"):
        if found != report_path and _is_fresh(found):
            logger.warning("Report found at %s, moving to %s", found, report_path)
            found.rename(report_path)
            return True

    logger.error(
        "Report file not freshly written at %s after report iteration "
        "(exists=%s, agent output: %.200s)",
        report_path,
        report_path.exists(),
        report_result.output,
    )
    return False


async def _recorded_run_environment(job_dir: Path) -> str | None:
    """The environment the run recorded, as the report footer names it, or None
    when it recorded nothing or the record cannot be read."""
    try:
        async with asyncio.timeout(_STAMP_DEADLINE_SECONDS):
            async with async_session_local() as session:
                version_info = (
                    await session.execute(
                        select(JobModel.version_info).where(JobModel.id == UUID(job_dir.name))
                    )
                ).scalar_one_or_none()
    except Exception as e:
        logger.warning("Could not read version provenance for job %s: %s", job_dir.name, e)
        return None
    return run_environment_text(version_info)


async def _try_generate_report_pdf(report_path: Path) -> None:
    """Generate HTML and PDF from the markdown report.

    Falls back to fpdf2 if WeasyPrint is unavailable or fails.
    """
    job_dir = report_path.parent
    html_path = job_dir / "final_report.html"
    pdf_path = job_dir / "final_report.pdf"
    run_environment = await _recorded_run_environment(job_dir)

    try:
        from openscientist.report.pdf import render_report_pdf
        from openscientist.report.renderer import render_report_html

        # Render HTML with file:// image paths (for WeasyPrint)
        html_content = render_report_html(
            report_path,
            job_dir,
            run_environment=run_environment,
            public_reader=True,
        )
        html_path.write_text(html_content, encoding="utf-8")
        logger.info("HTML report written: %s", html_path)

        await render_report_pdf(html_path, pdf_path, job_dir)
        return

    except Exception as exc:
        logger.warning("WeasyPrint PDF generation failed, falling back to fpdf2: %s", exc)

    try:
        from openscientist.pdf_generator import markdown_to_pdf
        from openscientist.report.processor import strip_figure_tags

        raw_md = report_path.read_text(encoding="utf-8")
        stripped = strip_figure_tags(raw_md)
        stripped_path = job_dir / "_final_report_stripped.md"
        stripped_path.write_text(stripped, encoding="utf-8")
        try:
            markdown_to_pdf(
                stripped_path, pdf_path, add_footer=True, run_environment=run_environment
            )
        finally:
            stripped_path.unlink(missing_ok=True)
    except Exception as fallback_exc:
        logger.warning("fpdf2 fallback also failed: %s", fallback_exc)


# A weak model sometimes ends a turn without producing the deliverable (it
# describes the report instead of writing the file). Re-ask in the same session
# a bounded number of times, and fail the job honestly if the attempts are exhausted.
_MAX_REPORT_ATTEMPTS = 3
_MAX_CONSENSUS_ATTEMPTS = 3


def _prompt_too_long(result: IterationResult) -> bool:
    """Whether the turn was refused for overflowing the model's context. The
    Claude Code CLI answers with this text, and the API error that other
    harnesses relay carries it too."""
    return "prompt is too long" in f"{result.output}\n{result.error}".lower()


async def _last_known_context_tokens(job_id: str) -> int | None:
    """The context the job's latest measured model call carried, as its input,
    cache-read and cache-write tokens. A call refused for its size is not
    billed, so it leaves no row, and an estimated row measures nothing."""
    try:
        async with asyncio.timeout(_STAMP_DEADLINE_SECONDS):
            async with async_session_local() as session:
                context: int | None = await session.scalar(
                    select(
                        CostRecord.input_tokens
                        + CostRecord.cache_read_tokens
                        + CostRecord.cache_write_tokens
                    )
                    .where(
                        CostRecord.job_id == UUID(job_id),
                        ~CostRecord.operation_type.endswith("_estimated", autoescape=True),
                    )
                    .order_by(CostRecord.created_at.desc(), CostRecord.id.desc())
                    .limit(1)
                )
                return context
    except Exception as e:
        logger.warning("Could not read the last context size of job %s: %s", job_id, e)
        return None


async def _run_report_turn(
    executor: AbstractAgent[Provider],
    job_dir: Path,
    research_question: str,
    ks: KnowledgeState,
    description: str | None,
) -> tuple[IterationResult, bool]:
    """Run the report turn, re-asking until the model creates final_report.md.

    The turn continues the agent's existing session, because a weak model drops
    into chat mode when handed a large "write this" prompt as its first message.
    A regeneration run, which has no prior session, starts one here. A turn
    refused for overflowing the context retries in a fresh session, its outline
    budgeted for half the context the job's last measured call carried.
    """
    report_path = job_dir / "final_report.md"
    # Snapshot the existing report's mtime (None if it does not exist) so a
    # stale file from a prior run cannot be mistaken for this turn's output.
    baseline_mtime_ns = report_path.stat().st_mtime_ns if report_path.exists() else None
    file_write_tool = executor.file_write_tool
    budget_tokens = executor.model_profile.context_window_tokens

    def report_prompt() -> str:
        return build_report_prompt(
            research_question,
            ks,
            job_dir=job_dir,
            description=description,
            file_write_tool=file_write_tool,
            context_window_tokens=budget_tokens,
        )

    prompt = report_prompt()
    logger.info("Report generation turn (prompt: %d chars)", len(prompt))

    result = await executor.run_iteration(prompt, reset_session=False)
    overflow: str | None = None
    for attempt in range(1, _MAX_REPORT_ATTEMPTS + 1):
        if _ensure_report_written(report_path, result, baseline_mtime_ns=baseline_mtime_ns):
            if attempt > 1:
                logger.info("Report written on attempt %d", attempt)
            return result, True
        overflow = None
        if _prompt_too_long(result):
            known = await _last_known_context_tokens(job_dir.name)
            size = f"last known context {known} tokens" if known else "no context size known"
            overflow = f"The report prompt was too long for the model's context ({size})."
            if attempt == _MAX_REPORT_ATTEMPTS:
                break
            budget_tokens = min(budget_tokens, known or budget_tokens) // 2
            logger.warning(
                "Report attempt %d/%d was too long for the model's context (%s); "
                "retrying in a fresh session with the prompt budgeted for %d tokens",
                attempt,
                _MAX_REPORT_ATTEMPTS,
                size,
                budget_tokens,
            )
            result = await executor.run_iteration(report_prompt(), reset_session=True)
            continue
        if attempt == _MAX_REPORT_ATTEMPTS:
            break
        logger.warning(
            "Report file missing after attempt %d/%d; re-asking", attempt, _MAX_REPORT_ATTEMPTS
        )
        result = await executor.run_iteration(
            build_report_retry_prompt(
                research_question,
                ks,
                job_dir=job_dir,
                description=description,
                file_write_tool=file_write_tool,
                context_window_tokens=budget_tokens,
            ),
            reset_session=False,
        )
    if overflow is not None:
        logger.error("Report not written after %d attempts: %s", _MAX_REPORT_ATTEMPTS, overflow)
        return replace(result, error=overflow), False
    logger.error("Report file not written after %d attempts", _MAX_REPORT_ATTEMPTS)
    return result, False


async def _set_consensus_answer(
    executor: AbstractAgent[Provider], job_dir: Path, research_question: str
) -> None:
    """Run the consensus turn, re-asking until the model records a fresh answer.

    A freshness guard mirrors the report file's. Only a value written this
    turn is accepted, so a regenerated report cannot ship the stale consensus.
    If the attempts are exhausted the job completes with the prior consensus
    (logged), rather than fabricating one.
    """
    baseline = (await KnowledgeState.load_from_database(job_dir.name)).data.get("consensus_answer")
    tool_prefix = executor.prompt_fragments().mcp_tool_prefix
    for attempt in range(1, _MAX_CONSENSUS_ATTEMPTS + 1):
        prompt = (
            build_consensus_prompt(research_question, tool_prefix=tool_prefix)
            if attempt == 1
            else build_consensus_retry_prompt(research_question, tool_prefix=tool_prefix)
        )
        await executor.run_iteration(prompt, reset_session=False)
        current = (await KnowledgeState.load_from_database(job_dir.name)).data.get(
            "consensus_answer"
        )
        if current and current != baseline:
            if attempt > 1:
                logger.info("Consensus recorded on attempt %d", attempt)
            return
        if attempt < _MAX_CONSENSUS_ATTEMPTS:
            logger.warning(
                "Consensus not recorded after attempt %d/%d; re-asking",
                attempt,
                _MAX_CONSENSUS_ATTEMPTS,
            )
    logger.warning("Consensus answer not recorded after %d attempts", _MAX_CONSENSUS_ATTEMPTS)


async def _begin_report_phase(job_dir: Path) -> None:
    """Move the job to generating_report, unless it stopped running meanwhile."""
    from openscientist.orchestrator.discovery import _DiscoveryCancelledError

    if not await update_job_status(job_dir, "generating_report", allowed_from=("running",)):
        raise _DiscoveryCancelledError(f"Job {job_dir.name} is no longer running")


async def _run_report_generation_phase(
    executor: AbstractAgent[Provider],
    job_dir: Path,
    research_question: str,
    description: str | None = None,
) -> _ReportOutcome:
    """Run the report and consensus turns (each with bounded retries) and output
    artifact handling, with the job in generating_report."""
    await _begin_report_phase(job_dir)
    ks = await KnowledgeState.load_from_database(job_dir.name)
    report_result, report_success = await _run_report_turn(
        executor, job_dir, research_question, ks, description
    )
    _save_report_transcript(job_dir, report_result.transcript)
    report_path = job_dir / "final_report.md"

    if report_success:
        # A dedicated consensus turn, separate from the report so a weaker
        # model commits fully to one deliverable at a time.
        await _set_consensus_answer(executor, job_dir, research_question)

        try:
            await _try_generate_report_pdf(report_path)
        except (ValueError, OSError, OpenScientistError) as exc:
            logger.warning("PDF generation failed: %s", exc)

    return _ReportOutcome(success=report_success, error=report_result.error)


async def _persist_final_status(
    job_dir: Path,
    report_outcome: _ReportOutcome,
    stop_note: str | None = None,
) -> str:
    """Persist final job status based on report generation outcome."""
    final_status = "completed" if report_outcome.success else "failed"
    if final_status == "completed":
        await update_job_status(
            job_dir, "completed", stop_note=stop_note, allowed_from=("generating_report",)
        )
    else:
        await update_job_status(
            job_dir,
            "failed",
            error_message=f"Report generation failed: {report_outcome.error}",
            allowed_from=("generating_report",),
        )
    return final_status
