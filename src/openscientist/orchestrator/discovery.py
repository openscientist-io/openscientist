"""
Async discovery loop for OpenScientist autonomous research.

The public entry point is run_discovery_async(), which the JobManager thread
calls via asyncio.run().
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from openscientist.agent.base import (
    AbstractAgent,
    AgentConfig,
    IterationResult,
    TurnOutcome,
)
from openscientist.agent.factory import agent_class_for_provider_id, get_agent
from openscientist.artifact_packager import ITERATION_LOG_FILE, TRANSCRIPT_SUFFIX
from openscientist.database.models import JobDataFile
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import async_session_local
from openscientist.exec_broker_client import ask_budget
from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import (
    FeedbackWaitResult,
    _get_job_status,
    build_initial_prompt,
    build_iteration_prompt,
    increment_ks_iteration,
    update_job_status,
    wait_for_feedback_or_timeout,
)
from openscientist.orchestrator.provenance import _sync_version_metadata
from openscientist.orchestrator.report import _persist_final_status, _run_report_generation_phase
from openscientist.providers.base import Provider
from openscientist.settings import get_settings
from openscientist.transcript import TranscriptEntry, save_transcript
from openscientist.version import get_version_string

logger = logging.getLogger(__name__)


class _DiscoveryCancelledError(RuntimeError):
    """Raised when a job is cancelled during discovery execution."""


class _BudgetStopError(RuntimeError):
    """Raised when the web's budget check says the job must stop iterating.

    ``note`` is the message the job ends with, naming the limit or why the
    budget could not be checked.
    """

    def __init__(self, note: str) -> None:
        super().__init__(note)
        self.note = note


async def _check_budget_before_iteration(job_id: str) -> None:
    """Ask the web whether the job may run its next iteration.

    Raises:
        _BudgetStopError: A limit was reached, or the check could not be
            answered in time, carrying the note the job ends with.
    """
    verdict = await ask_budget(job_id=job_id)
    if not verdict.ok:
        raise _BudgetStopError(verdict.message or "The budget could not be checked")


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
    _save_transcript(provenance_dir / f"iter{iteration}{TRANSCRIPT_SUFFIX}", result.transcript)
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
    await _check_budget_before_iteration(job_id)
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
        await _check_budget_before_iteration(job_id)
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

        result = await executor.run_iteration(iteration_prompt, reset_session=should_reset)
        _check_turn_outcome(result, iteration)
        _append_iteration_artifacts(
            provenance_dir=provenance_dir,
            log_file=log_file,
            iteration=iteration,
            prompt=iteration_prompt,
            result=result,
        )
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


async def _finalize_executor(executor: AbstractAgent[Provider]) -> None:
    """Shut the executor down. Shared ``finally`` handling for both the full
    discovery run and the report-only regeneration run so neither leaks the
    executor. The cost of what it spent goes to the rows the LLM proxy wrote,
    one per model call."""
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
    try:
        report_outcome = await _run_report_generation_phase(
            executor=executor,
            job_dir=job_dir,
            research_question=runtime["research_question"],
            description=runtime.get("description"),
        )
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
        await _finalize_executor(executor)


async def run_discovery_async(job_dir: Path) -> dict[str, Any]:
    """Run autonomous discovery using the configured agent executor.

    Called by JobManager or the container entrypoint.
    """
    job_dir = Path(job_dir)
    runtime = await _load_runtime_context(job_dir)
    job_id = runtime["job_id"]
    logger.info("Starting discovery for job %s (mode=%s)", job_id, runtime["investigation_mode"])

    executor = await _build_and_prepare_executor(job_dir, runtime)
    logger.info("Created agent executor for job %s", job_id)

    provenance_dir = job_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    log_file = job_dir / ITERATION_LOG_FILE

    try:
        # A budget limit (or a check the web could not answer in time) stops the
        # iterations but not the report, so the note is captured and the report
        # phase still runs.
        stop_note: str | None = None
        try:
            await _run_primary_discovery_loop(
                executor=executor,
                job_dir=job_dir,
                runtime=runtime,
                provenance_dir=provenance_dir,
                log_file=log_file,
            )
        except _BudgetStopError as stopped:
            stop_note = stopped.note
            logger.info("Discovery stopped at a budget limit for job %s: %s", job_id, stopped.note)
        report_outcome = await _run_report_generation_phase(
            executor=executor,
            job_dir=job_dir,
            research_question=runtime["research_question"],
            description=runtime.get("description"),
        )
        final_status = await _persist_final_status(job_dir, report_outcome, stop_note=stop_note)
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
        await _finalize_executor(executor)


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
