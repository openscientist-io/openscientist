"""
Async discovery loop for OpenScientist autonomous research.

The public entry point is run_discovery_async(), which the JobManager thread
calls via asyncio.run().
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text, update

from openscientist.agent.base import (
    AbstractAgent,
    AgentBackend,
    AgentConfig,
    IterationResult,
    TokenUsage,
    TurnOutcome,
)
from openscientist.agent.factory import agent_class_for_provider_id, get_agent
from openscientist.database.models import JobDataFile
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import async_session_local
from openscientist.exceptions import OpenScientistError
from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import (
    FeedbackWaitResult,
    _get_job_status,
    build_consensus_prompt,
    build_consensus_retry_prompt,
    build_initial_prompt,
    build_iteration_prompt,
    build_report_prompt,
    build_report_retry_prompt,
    increment_ks_iteration,
    update_job_status,
    wait_for_feedback_or_timeout,
)
from openscientist.providers.base import Provider
from openscientist.settings import get_settings
from openscientist.transcript import TranscriptEntry, save_transcript
from openscientist.version import get_version_string

logger = logging.getLogger(__name__)

# Provenance is a single small update, so anything slower is a database in
# trouble, and the run should carry on without the stamp. The statement bound
# lives in the database. The deadline covers what the database cannot see.
_STAMP_TIMEOUT_MS = 10_000
_STAMP_DEADLINE_SECONDS = 30.0


class _DiscoveryCancelledError(RuntimeError):
    """Raised when a job is cancelled during discovery execution."""


@dataclass(frozen=True)
class _ReportOutcome:
    """Outcome of report-generation phase."""

    success: bool
    error: str


def _resolve_primary_data_file(data_files: list[str]) -> Path | None:
    """Resolve the primary data file path for the agent executor."""
    if not data_files:
        return None
    data_file = Path(data_files[0])
    if not data_file.is_absolute():
        return data_file.absolute()
    return data_file


def _build_agent_executor(
    job_dir: Path,
    data_file: Path | None,
    *,
    use_hypotheses: bool = False,
    data_files: list[Path] | None = None,
) -> AbstractAgent[Provider]:
    """Create a configured agent for discovery/report phases.

    The backend that drives the configured provider chooses its own discovery
    system prompt: Claude returns a concise prompt (its rich ``CLAUDE.md`` is
    written separately into ``.claude/`` by ``prepare_job_workspace``), codex
    returns the full per-job doc delivered via ``AGENTS.md``.
    """
    agent_cls = agent_class_for_provider_id(get_settings().provider.provider_id)
    system_prompt = agent_cls.discovery_system_prompt(
        use_hypotheses=use_hypotheses,
        phenix_available=get_settings().phenix.is_available,
    )
    logger.info("Built %s system prompt (%d chars)", agent_cls.backend.value, len(system_prompt))
    config = AgentConfig(
        job_dir=job_dir,
        data_file=data_file,
        system_prompt=system_prompt,
        use_hypotheses=use_hypotheses,
        data_files=tuple(data_files or ()),
    )
    return get_agent(config)


def _append_iteration_artifacts(
    *,
    provenance_dir: Path,
    log_file: Path,
    iteration: int,
    prompt: str,
    result: IterationResult,
    overwrite_log: bool = False,
) -> None:
    """Persist transcript and log entry for a completed iteration."""
    _save_transcript(provenance_dir / f"iter{iteration}_transcript.json", result.transcript)
    _append_log(
        log_file,
        iteration,
        prompt,
        result.output,
        result.tool_calls,
        write=overwrite_log,
        timed_out=result.outcome is TurnOutcome.TIMED_OUT,
    )


def _check_turn_outcome(result: IterationResult, iteration: int) -> None:
    """Apply the loop's per-turn policy.

    FAILED aborts the run. TIMED_OUT is recorded and the loop advances: the
    turn was cut by a wall-clock timeout, and any work done before the cut is
    already persisted via the tools, so a stalled model is surfaced (in the log
    and the honest outcome) rather than silently passed off as success.
    """
    if result.outcome is TurnOutcome.FAILED:
        logger.error("Iteration %d failed: %s", iteration, result.error)
        raise RuntimeError(f"Iteration {iteration} failed: {result.error}")
    if result.outcome is TurnOutcome.TIMED_OUT:
        logger.warning(
            "Iteration %d timed out (tool_calls=%d); advancing, work before the cut is persisted",
            iteration,
            result.tool_calls,
        )
    else:
        logger.info("Iteration %d completed (tool_calls=%d)", iteration, result.tool_calls)


async def _sync_version_metadata(job_id: str) -> None:
    """Record runtime version provenance on the job, best effort.

    Two bounds apply to the write because neither covers the other.
    ``statement_timeout`` stops a slow statement inside the database, and the
    surrounding deadline covers what the database cannot see (name resolution,
    connecting, acquiring a connection, cleanup). Metadata collection runs in
    a worker thread, and a failure there is still caught here.
    """
    try:
        async with asyncio.timeout(_STAMP_DEADLINE_SECONDS):
            version_info = await asyncio.to_thread(get_version_metadata)
            if not version_info:
                return
            async with async_session_local() as session:
                await session.execute(text(f"SET LOCAL statement_timeout = {_STAMP_TIMEOUT_MS}"))
                await session.execute(
                    update(JobModel)
                    .where(JobModel.id == UUID(job_id))
                    .values(version_info=version_info)
                )
                await session.commit()
    except Exception as e:
        logger.warning("Could not record version provenance for job %s: %s", job_id, e)


async def _wait_for_coinvestigate_feedback(
    job_dir: Path,
    investigation_mode: str,
    current_iteration: int,
    max_iterations: int,
) -> FeedbackWaitResult | None:
    """Pause for user feedback between iterations in co-investigation mode."""
    if investigation_mode != "coinvestigate" or current_iteration >= max_iterations:
        return None
    if not await update_job_status(job_dir, "awaiting_feedback", allowed_from=("running",)):
        raise _DiscoveryCancelledError(f"Job {job_dir.name} is no longer running")
    wait_result = await wait_for_feedback_or_timeout(job_dir)
    if wait_result["outcome"] != "cancelled" and not await update_job_status(
        job_dir, "running", allowed_from=("awaiting_feedback",)
    ):
        # The feedback panel resumed the job first, or the job ended while it waited.
        await _assert_job_running(job_dir.name)
    return wait_result


async def _assert_job_not_cancelled(job_id: str) -> None:
    """Raise if the job was cancelled by the user."""
    status = await _get_job_status(job_id)
    if status == "cancelled":
        raise _DiscoveryCancelledError(f"Job {job_id} was cancelled")


async def _assert_job_running(job_id: str) -> None:
    """Raise if the job is no longer running, so the container stops."""
    status = await _get_job_status(job_id)
    if status != "running":
        raise _DiscoveryCancelledError(f"Job {job_id} is {status}")


async def _run_primary_discovery_loop(
    *,
    executor: AbstractAgent[Provider],
    job_dir: Path,
    runtime: dict[str, Any],
    provenance_dir: Path,
    log_file: Path,
    ledger: _CostLedger,
) -> None:
    """Run initial and iterative discovery phases before report generation."""
    job_id = runtime["job_id"]
    max_iterations = runtime["max_iterations"]
    data_files = runtime["data_files"]
    investigation_mode = runtime["investigation_mode"]

    ks = await KnowledgeState.load_from_database(job_id)
    initial_prompt = build_initial_prompt(
        runtime["research_question"],
        max_iterations,
        data_files,
        ks,
        description=runtime.get("description"),
        tool_prefix=executor.prompt_fragments().mcp_tool_prefix,
    )

    logger.info("Iteration 1/%d: Starting session", max_iterations)
    ledger.begin("discovery", 1)
    result = await executor.run_iteration(initial_prompt, reset_session=True)
    _check_turn_outcome(result, 1)

    await _sync_version_metadata(job_id)
    _append_iteration_artifacts(
        provenance_dir=provenance_dir,
        log_file=log_file,
        iteration=1,
        prompt=initial_prompt,
        result=result,
        overwrite_log=True,
    )
    await ledger.record()
    if max_iterations > 1:
        await increment_ks_iteration(job_id)
    await _assert_job_not_cancelled(job_id)

    pending_feedback_result = await _wait_for_coinvestigate_feedback(
        job_dir,
        investigation_mode,
        current_iteration=1,
        max_iterations=max_iterations,
    )
    if pending_feedback_result and pending_feedback_result["outcome"] == "cancelled":
        raise _DiscoveryCancelledError(f"Job {job_id} was cancelled")
    pending_feedback = (
        pending_feedback_result["feedback_text"]
        if pending_feedback_result and pending_feedback_result["outcome"] == "feedback"
        else None
    )
    reset_interval = 5

    for iteration in range(2, max_iterations + 1):
        await _assert_job_not_cancelled(job_id)
        ks = await KnowledgeState.load_from_database(job_id)
        if pending_feedback is None:
            pending_feedback = ks.get_feedback_for_iteration(iteration)

        iteration_prompt = build_iteration_prompt(
            iteration,
            max_iterations,
            ks,
            pending_feedback,
            description=runtime.get("description"),
            tool_prefix=executor.prompt_fragments().mcp_tool_prefix,
        )
        pending_feedback = None
        should_reset = iteration % reset_interval == 1
        logger.info(
            "Iteration %d/%d (%s)",
            iteration,
            max_iterations,
            "fresh session" if should_reset else "continuing",
        )

        ledger.begin("discovery", iteration)
        result = await executor.run_iteration(iteration_prompt, reset_session=should_reset)
        _check_turn_outcome(result, iteration)
        _append_iteration_artifacts(
            provenance_dir=provenance_dir,
            log_file=log_file,
            iteration=iteration,
            prompt=iteration_prompt,
            result=result,
        )
        await ledger.record()

        if iteration < max_iterations:
            await increment_ks_iteration(job_id)
        await _assert_job_not_cancelled(job_id)
        pending_feedback_result = await _wait_for_coinvestigate_feedback(
            job_dir,
            investigation_mode,
            current_iteration=iteration,
            max_iterations=max_iterations,
        )
        if pending_feedback_result and pending_feedback_result["outcome"] == "cancelled":
            raise _DiscoveryCancelledError(f"Job {job_id} was cancelled")
        pending_feedback = (
            pending_feedback_result["feedback_text"]
            if pending_feedback_result and pending_feedback_result["outcome"] == "feedback"
            else None
        )

    logger.info("Discovery loop completed")


def _save_report_transcript(job_dir: Path, transcript: list[TranscriptEntry]) -> None:
    """Persist report-generation transcript artifact."""
    provenance_dir = job_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    _save_transcript(provenance_dir / "report_transcript.json", transcript)


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


async def _try_generate_report_pdf(report_path: Path) -> None:
    """Generate HTML and PDF from the markdown report.

    Falls back to fpdf2 if WeasyPrint is unavailable or fails.
    """
    job_dir = report_path.parent
    html_path = job_dir / "final_report.html"
    pdf_path = job_dir / "final_report.pdf"

    try:
        from openscientist.report.pdf import render_report_pdf
        from openscientist.report.renderer import render_report_html

        # Render HTML with file:// image paths (for WeasyPrint)
        html_content = render_report_html(report_path, job_dir)
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
            markdown_to_pdf(stripped_path, pdf_path, add_footer=True)
        finally:
            stripped_path.unlink(missing_ok=True)
    except Exception as fallback_exc:
        logger.warning("fpdf2 fallback also failed: %s", fallback_exc)


# A weak model sometimes ends a turn without producing the deliverable (it
# describes the report instead of writing the file). Re-ask in the same session
# a bounded number of times, and fail the job honestly if the attempts are exhausted.
_MAX_REPORT_ATTEMPTS = 3
_MAX_CONSENSUS_ATTEMPTS = 3


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
    A regeneration run, which has no prior session, starts one here.
    """
    report_path = job_dir / "final_report.md"
    # Snapshot the existing report's mtime (None if it does not exist) so a
    # stale file from a prior run cannot be mistaken for this turn's output.
    baseline_mtime_ns = report_path.stat().st_mtime_ns if report_path.exists() else None
    file_write_tool = executor.file_write_tool
    context_window_tokens = executor.model_profile.context_window_tokens
    prompt = build_report_prompt(
        research_question,
        ks,
        job_dir=job_dir,
        description=description,
        file_write_tool=file_write_tool,
        context_window_tokens=context_window_tokens,
    )
    logger.info("Report generation turn (prompt: %d chars)", len(prompt))

    result = await executor.run_iteration(prompt, reset_session=False)
    for attempt in range(1, _MAX_REPORT_ATTEMPTS + 1):
        if _ensure_report_written(report_path, result, baseline_mtime_ns=baseline_mtime_ns):
            if attempt > 1:
                logger.info("Report written on attempt %d", attempt)
            return result, True
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
                context_window_tokens=context_window_tokens,
            ),
            reset_session=False,
        )
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
) -> str:
    """Persist final job status based on report generation outcome."""
    final_status = "completed" if report_outcome.success else "failed"
    if final_status == "completed":
        await update_job_status(job_dir, "completed", allowed_from=("generating_report",))
    else:
        await update_job_status(
            job_dir,
            "failed",
            error_message=f"Report generation failed: {report_outcome.error}",
            allowed_from=("generating_report",),
        )
    return final_status


async def _load_runtime_context(job_dir: Path) -> dict[str, Any]:
    """Load runtime job metadata from the database."""
    job_uuid = UUID(job_dir.name)

    async with async_session_local() as session:
        job_result = await session.execute(select(JobModel).where(JobModel.id == job_uuid))
        job = job_result.scalar_one_or_none()
        if job is None:
            raise ValueError(f"Job {job_uuid} not found in database")

        files_result = await session.execute(
            select(JobDataFile.file_path)
            .where(JobDataFile.job_id == job_uuid)
            .order_by(JobDataFile.created_at.asc())
        )
        data_files = [str(path) for path in files_result.scalars().all()]

    resolved_files: list[str] = []
    for raw_path in data_files:
        file_path = Path(raw_path)
        if not file_path.is_absolute():
            file_path = job_dir / file_path
        resolved_files.append(str(file_path))

    return {
        "job_id": str(job.id),
        "research_question": job.research_question,
        "description": getattr(job, "description", None),
        "max_iterations": job.max_iterations,
        "use_hypotheses": bool(job.use_hypotheses),
        "investigation_mode": job.investigation_mode,
        "data_files": resolved_files,
    }


def _harness_binary(harness: AgentBackend) -> str:
    """The binary the harness agent will launch: the one the codex SDK is pinned
    to, or omp through its own resolver so env overrides stay honoured.
    """
    if harness is AgentBackend.CODEX:
        from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]

        return str(bundled_codex_path())
    from openscientist.agent.omp_agent import _resolve_omp_bin

    return _resolve_omp_bin()


def _harness_cli_version(command: str) -> str | None:
    """Bare version from the first ``<command> --version`` line; None if the CLI is unusable."""
    try:
        result = subprocess.run(
            [command, "--version"], capture_output=True, text=True, timeout=3, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first_line = result.stdout.strip().split("\n", 1)[0]
    match = re.search(r"\d+\.\d+\S*", first_line)
    return match.group(0) if match else (first_line or None)


def get_version_metadata() -> dict[str, str]:
    """Get OpenScientist version metadata for reproducibility."""
    from openscientist.version import SHORT_COMMIT_LENGTH, get_commit

    metadata: dict[str, str] = {}

    commit = get_commit()
    if commit != "unknown":
        metadata["openscientist_commit"] = commit

    openscientist_build_time = os.environ.get("OPENSCIENTIST_BUILD_TIME")  # env-ok
    if openscientist_build_time and openscientist_build_time != "unknown":
        metadata["openscientist_build_time"] = openscientist_build_time

    try:
        if Path("/.dockerenv").exists():
            with open("/etc/hostname", encoding="utf-8") as f:
                container_id = f.read().strip()
                if container_id:
                    metadata["docker_container_id"] = container_id[:SHORT_COMMIT_LENGTH]
    except OSError:
        pass

    # The resolved harness driving the job, never the literal "auto". A
    # failure here also aborts agent build, so the keys stay unrecorded.
    try:
        harness = agent_class_for_provider_id(get_settings().provider.provider_id).backend
    except Exception:
        return metadata
    metadata["agent_harness"] = harness.value

    if harness is AgentBackend.CLAUDE_CODE:
        # Private modules of an unpinned SDK; omit each key if its module moves.
        try:
            from claude_agent_sdk._cli_version import __cli_version__

            metadata["claude_code_version"] = __cli_version__
            metadata["agent_harness_version"] = __cli_version__
        except Exception:
            pass

        try:
            from claude_agent_sdk._version import __version__

            metadata["claude_agent_sdk_version"] = __version__
        except Exception:
            pass
    elif version := _harness_cli_version(_harness_binary(harness)):
        metadata["agent_harness_version"] = version

    return metadata


async def _persist_job_cost_record(
    job_id: str,
    tokens: TokenUsage,
    provider_name: str,
    model_name: str,
    operation_type: str = "discovery",
    iteration: int | None = None,
) -> None:
    """Write a CostRecord for ``tokens``, spent in ``operation_type`` at ``iteration``."""
    from openscientist.database.models import CostRecord
    from openscientist.providers.pricing import estimate_cost_usd

    # The pricing buckets are keyword-only, since they are easy to transpose and each
    # is priced differently.
    cost_usd = estimate_cost_usd(
        model_name,
        input_tokens=tokens.input_tokens,
        output_tokens=tokens.output_tokens,
        cache_read_tokens=tokens.cache_read_tokens,
        cache_write_tokens=tokens.cache_write_tokens,
        cache_write_1h_tokens=tokens.cache_write_1h_tokens,
        reasoning_tokens=tokens.reasoning_tokens,
    )
    async with async_session_local() as session:
        record = CostRecord(
            job_id=UUID(job_id),
            iteration=iteration,
            operation_type=operation_type,
            provider=provider_name,
            model=model_name,
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cache_read_tokens=tokens.cache_read_tokens,
            # The column holds every cache write. Only the price depends on how long
            # the entry lives, and `cost_usd` already accounts for that.
            cache_write_tokens=tokens.cache_write_tokens + tokens.cache_write_1h_tokens,
            reasoning_tokens=tokens.reasoning_tokens,
            cost_usd=cost_usd,
        )
        session.add(record)
        await session.commit()


class _CostLedger:
    """A run's cost, written as it goes. Each record holds the tokens spent
    since the last record that was written, so a failed write is carried into
    the next one."""

    def __init__(
        self,
        job_id: str,
        *,
        usage: Callable[[], TokenUsage],
        provider_name: str,
        model_name: str,
    ) -> None:
        self._job_id = job_id
        self._usage = usage
        self._provider_name = provider_name
        self._model_name = model_name
        self._written = TokenUsage()
        self._operation_type = "discovery"
        self._iteration: int | None = None

    @classmethod
    def for_executor(cls, executor: AbstractAgent[Provider], job_id: str) -> _CostLedger:
        settings = get_settings()
        provider = executor.provider
        # A chat turn can run on its own model (model_override), which then
        # sets the price.
        model_name = (
            executor.config.model_override
            or settings.provider.model
            or settings.provider.anthropic_default_sonnet_model
            or provider.effective_model_name()
            or "unknown"
        )
        return cls(
            job_id,
            usage=lambda: executor.total_tokens,
            provider_name=provider.display_name,
            model_name=model_name,
        )

    def begin(self, operation_type: str, iteration: int | None) -> None:
        """Label the tokens spent from now on."""
        self._operation_type = operation_type
        self._iteration = iteration

    def _unwritten(self) -> tuple[TokenUsage, TokenUsage]:
        # A copy, since agents grow their running total in place.
        total = replace(self._usage())
        return total, total - self._written

    async def record(self) -> bool:
        """Write the tokens spent since the last written record. Returns whether
        nothing is left unwritten."""
        total, unwritten = self._unwritten()
        if unwritten == TokenUsage():
            return True
        try:
            await _persist_job_cost_record(
                self._job_id,
                unwritten,
                self._provider_name,
                self._model_name,
                operation_type=self._operation_type,
                iteration=self._iteration,
            )
        except Exception as e:
            logger.warning(
                "Could not write the cost of job %s, carrying it to the next record: %s",
                self._job_id,
                e,
            )
            return False
        self._written = total
        return True

    async def close(self) -> None:
        """Write what is left. If that fails too, log every count so the
        tokens can still be billed."""
        if await self.record():
            return
        _, unwritten = self._unwritten()
        logger.error(
            "Cost of job %s was not saved: provider=%s model=%s input=%d output=%d "
            "cache_write=%d cache_write_1h=%d cache_read=%d reasoning=%d",
            self._job_id,
            self._provider_name,
            self._model_name,
            unwritten.input_tokens,
            unwritten.output_tokens,
            unwritten.cache_write_tokens,
            unwritten.cache_write_1h_tokens,
            unwritten.cache_read_tokens,
            unwritten.reasoning_tokens,
        )


async def _finalize_executor(executor: AbstractAgent[Provider], ledger: _CostLedger) -> None:
    """Log token usage, write the cost not yet recorded, and shut the executor down.

    Shared ``finally`` handling for both the full discovery run and the
    report-only regeneration run so neither leaks the executor or its cost.
    """
    tokens = executor.total_tokens
    logger.info(
        "Agent executor completed: %d input, %d output, %d cache read, "
        "%d cache write (%d of them one-hour), %d reasoning tokens",
        tokens.input_tokens,
        tokens.output_tokens,
        tokens.cache_read_tokens,
        tokens.cache_write_tokens + tokens.cache_write_1h_tokens,
        tokens.cache_write_1h_tokens,
        tokens.reasoning_tokens,
    )
    await ledger.close()
    await executor.shutdown()


async def _build_and_prepare_executor(
    job_dir: Path, runtime: dict[str, Any]
) -> AbstractAgent[Provider]:
    """Build the agent executor and run backend setup, marking the job running.

    Shared by the full discovery run and the report-only regeneration path.
    The backend applies its runtime env and materialises the per-job workspace
    before any turn runs.
    """
    use_hypotheses = runtime["use_hypotheses"]
    all_data_files = [Path(p) for p in runtime["data_files"]]
    executor = _build_agent_executor(
        job_dir=job_dir,
        data_file=_resolve_primary_data_file(runtime["data_files"]),
        use_hypotheses=use_hypotheses,
        data_files=all_data_files,
    )
    executor.apply_runtime_environment()
    if not await update_job_status(job_dir, "running", allowed_from=("running",)):
        raise _DiscoveryCancelledError(f"Job {job_dir.name} is no longer running")
    await executor.prepare_job_workspace(use_hypotheses=use_hypotheses)
    # Resolve the model's context window once per job, off the event loop (the
    # Ollama probe is blocking I/O). Cached on the agent for the report budget.
    await executor.warm_model_profile()
    return executor


async def regenerate_report_async(job_dir: Path) -> dict[str, Any]:
    """Re-run only the report-generation phase for an already-finished job.

    Backs the admin "Regenerate report" action. The discovery iterations are
    not re-run, because every finding already lives in the persisted
    ``KnowledgeState`` and the report turn starts a fresh agent session. It
    overwrites ``final_report.md`` and its PDF and persists the final job
    status.
    """
    job_dir = Path(job_dir)
    runtime = await _load_runtime_context(job_dir)
    job_id = runtime["job_id"]
    logger.info("Regenerating report for job %s", job_id)

    executor = await _build_and_prepare_executor(job_dir, runtime)
    ledger = _CostLedger.for_executor(executor, job_id)
    try:
        ledger.begin("report", None)
        report_outcome = await _run_report_generation_phase(
            executor=executor,
            job_dir=job_dir,
            research_question=runtime["research_question"],
            description=runtime.get("description"),
        )
        await ledger.record()
        final_status = await _persist_final_status(job_dir, report_outcome)
        ks = await KnowledgeState.load_from_database(job_id)
        return {
            "job_id": job_id,
            "status": final_status,
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }
    except _DiscoveryCancelledError:
        logger.info("Report regeneration cancelled for job %s", job_id)
        ks = await KnowledgeState.load_from_database(job_id)
        return {
            "job_id": job_id,
            "status": "cancelled",
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }
    except Exception as e:
        logger.error("Report regeneration failed [%s]: %s", get_version_string(), e, exc_info=True)
        try:
            await update_job_status(
                job_dir,
                "failed",
                error_message=str(e),
                allowed_from=("running", "generating_report"),
            )
        except Exception as status_error:
            logger.warning("Failed to persist failure status for job %s: %s", job_id, status_error)
        try:
            ks = await KnowledgeState.load_from_database(job_id)
            iterations = ks.data["iteration"]
            findings = len(ks.data["findings"])
        except Exception:
            iterations = 0
            findings = 0
        return {
            "job_id": job_id,
            "status": "failed",
            "iterations": iterations,
            "findings": findings,
            "error": str(e),
        }
    finally:
        await _finalize_executor(executor, ledger)


async def run_discovery_async(job_dir: Path) -> dict[str, Any]:
    """Run autonomous discovery using the configured agent executor.

    Called by JobManager or the container entrypoint.
    """
    job_dir = Path(job_dir)
    runtime = await _load_runtime_context(job_dir)
    job_id = runtime["job_id"]
    logger.info("Starting discovery for job %s (mode=%s)", job_id, runtime["investigation_mode"])

    executor = await _build_and_prepare_executor(job_dir, runtime)
    ledger = _CostLedger.for_executor(executor, job_id)
    logger.info("Created agent executor for job %s", job_id)

    provenance_dir = job_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    log_file = job_dir / "claude_iterations.log"

    try:
        await _run_primary_discovery_loop(
            executor=executor,
            job_dir=job_dir,
            runtime=runtime,
            provenance_dir=provenance_dir,
            log_file=log_file,
            ledger=ledger,
        )
        ledger.begin("report", None)
        report_outcome = await _run_report_generation_phase(
            executor=executor,
            job_dir=job_dir,
            research_question=runtime["research_question"],
            description=runtime.get("description"),
        )
        await ledger.record()
        final_status = await _persist_final_status(job_dir, report_outcome)
        ks = await KnowledgeState.load_from_database(job_id)
        return {
            "job_id": job_id,
            "status": final_status,
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }

    except _DiscoveryCancelledError:
        logger.info("Discovery cancelled for job %s", job_id)
        ks = await KnowledgeState.load_from_database(job_id)
        return {
            "job_id": job_id,
            "status": "cancelled",
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }

    except Exception as e:
        logger.error("Discovery failed [%s]: %s", get_version_string(), e, exc_info=True)
        try:
            await update_job_status(
                job_dir,
                "failed",
                error_message=str(e),
                allowed_from=("running", "awaiting_feedback", "generating_report"),
            )
        except Exception as status_error:
            logger.warning("Failed to persist failure status for job %s: %s", job_id, status_error)
        try:
            ks = await KnowledgeState.load_from_database(job_id)
            iterations = ks.data["iteration"]
            findings = len(ks.data["findings"])
        except Exception:
            iterations = 0
            findings = 0
        return {
            "job_id": job_id,
            "status": "failed",
            "iterations": iterations,
            "findings": findings,
            "error": str(e),
        }

    finally:
        await _finalize_executor(executor, ledger)


def _save_transcript(path: Path, transcript: list[TranscriptEntry]) -> None:
    """Save iteration transcript to JSON file."""
    save_transcript(path, transcript)
    logger.info("Saved transcript to %s", path)


def _append_log(
    log_file: Path,
    iteration: int,
    prompt: str,
    output: str,
    tool_calls: int,
    write: bool = False,
    timed_out: bool = False,
) -> None:
    """Append iteration summary to the log file."""
    mode = "w" if write else "a"
    with open(log_file, mode, encoding="utf-8") as f:
        f.write(f"=== Iteration {iteration} ===\n")
        f.write(f"Prompt: {prompt}\n\n")
        f.write(f"Output: {output}\n\n")
        f.write(f"Tool calls: {tool_calls}\n\n")
        if timed_out:
            f.write("Timed out: yes (turn cut by the wall-clock limit)\n\n")
