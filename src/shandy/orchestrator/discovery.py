"""
Async discovery loop for SHANDY autonomous research.

The public entry point is run_discovery_async(), which the JobManager thread
calls via asyncio.run().
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from shandy.agent.factory import get_agent_executor
from shandy.agent.protocol import AgentExecutor, IterationResult, TokenUsage
from shandy.async_tasks import create_background_task
from shandy.database.models import JobDataFile
from shandy.database.models.job import Job as JobModel
from shandy.database.session import AsyncSessionLocal
from shandy.exceptions import ShandyError
from shandy.knowledge_state import KS_FILENAME, KnowledgeState
from shandy.orchestrator.iteration import (
    FeedbackWaitResult,
    _get_job_status,
    build_initial_prompt,
    build_iteration_prompt,
    build_report_prompt,
    increment_ks_iteration,
    update_job_status,
    wait_for_feedback_or_timeout,
)
from shandy.prompts import (
    get_enabled_skills,
    get_system_prompt,
)
from shandy.providers import get_provider
from shandy.settings import get_settings
from shandy.version import get_version_string

logger = logging.getLogger(__name__)


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
    use_hypotheses: bool = False,
    data_files: list[Path] | None = None,
) -> AgentExecutor:
    """Create a configured agent executor for discovery/report phases."""
    system_prompt = get_system_prompt()
    logger.info("Built system prompt (%d chars)", len(system_prompt))
    return get_agent_executor(
        job_dir=job_dir,
        data_file=data_file,
        system_prompt=system_prompt,
        use_hypotheses=use_hypotheses,
        data_files=data_files,
    )


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
    )


def _sync_version_metadata_if_available(job_dir: Path, ks_path: Path) -> None:
    """Store runtime version metadata in knowledge state when available."""
    version_info = get_version_metadata()
    if not version_info:
        return
    ks = KnowledgeState.load(ks_path)
    ks.set_version_info(version_info)
    ks.save(ks_path)
    sync_knowledge_state_to_db(job_dir, ks)


async def _wait_for_coinvestigate_feedback(
    job_dir: Path,
    investigation_mode: str,
    current_iteration: int,
    max_iterations: int,
) -> FeedbackWaitResult | None:
    """Pause for user feedback between iterations in co-investigation mode."""
    if investigation_mode != "coinvestigate" or current_iteration >= max_iterations:
        return None
    await update_job_status(job_dir, "awaiting_feedback")
    wait_result = await wait_for_feedback_or_timeout(job_dir)
    if wait_result["outcome"] != "cancelled":
        await update_job_status(job_dir, "running")
    return wait_result


async def _assert_job_not_cancelled(job_id: str) -> None:
    """Raise if the job was cancelled by the user."""
    status = await _get_job_status(job_id)
    if status == "cancelled":
        raise _DiscoveryCancelledError(f"Job {job_id} was cancelled")


async def _run_primary_discovery_loop(
    *,
    executor: AgentExecutor,
    job_dir: Path,
    runtime: dict[str, Any],
    ks_path: Path,
    provenance_dir: Path,
    log_file: Path,
) -> None:
    """Run initial and iterative discovery phases before report generation."""
    job_id = runtime["job_id"]
    max_iterations = runtime["max_iterations"]
    data_files = runtime["data_files"]
    investigation_mode = runtime["investigation_mode"]

    ks = KnowledgeState.load(ks_path)
    initial_prompt = build_initial_prompt(
        runtime["research_question"], max_iterations, data_files, ks
    )

    logger.info("Iteration 1/%d: Starting session", max_iterations)
    result = await executor.run_iteration(initial_prompt, reset_session=True)
    if not result.success:
        logger.error("Iteration 1 failed: %s", result.error)
        raise RuntimeError(f"Agent loop failed: {result.error}")
    logger.info("Iteration 1 completed (tool_calls=%d)", result.tool_calls)

    _sync_version_metadata_if_available(job_dir, ks_path)
    _append_iteration_artifacts(
        provenance_dir=provenance_dir,
        log_file=log_file,
        iteration=1,
        prompt=initial_prompt,
        result=result,
        overwrite_log=True,
    )
    if max_iterations > 1:
        increment_ks_iteration(ks_path)
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
        ks = KnowledgeState.load(ks_path)
        if pending_feedback is None:
            pending_feedback = ks.get_feedback_for_iteration(iteration)

        iteration_prompt = build_iteration_prompt(iteration, max_iterations, ks, pending_feedback)
        pending_feedback = None
        should_reset = iteration % reset_interval == 1
        logger.info(
            "Iteration %d/%d (%s)",
            iteration,
            max_iterations,
            "fresh session" if should_reset else "continuing",
        )

        result = await executor.run_iteration(iteration_prompt, reset_session=should_reset)
        if not result.success:
            logger.error("Iteration %d failed: %s", iteration, result.error)
            raise RuntimeError(f"Iteration {iteration} failed: {result.error}")

        logger.info("Iteration %d completed (tool_calls=%d)", iteration, result.tool_calls)
        _append_iteration_artifacts(
            provenance_dir=provenance_dir,
            log_file=log_file,
            iteration=iteration,
            prompt=iteration_prompt,
            result=result,
        )

        if iteration < max_iterations:
            increment_ks_iteration(ks_path)
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


def _save_report_transcript(job_dir: Path, transcript: list[dict[str, Any]]) -> None:
    """Persist report-generation transcript artifact."""
    provenance_dir = job_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    _save_transcript(provenance_dir / "report_transcript.json", transcript)


def _ensure_report_written(report_path: Path, report_result: IterationResult) -> bool:
    """Ensure final_report.md exists after report iteration."""
    if report_path.exists():
        return True
    if report_result.success and len(report_result.output) > 500:
        report_path.write_text(report_result.output, encoding="utf-8")
        return True
    return False


def _try_generate_report_pdf(report_path: Path) -> None:
    """Generate PDF from markdown report when possible."""
    from shandy.pdf_generator import markdown_to_pdf

    markdown_to_pdf(report_path, add_footer=True)


async def _run_report_generation_phase(
    executor: AgentExecutor,
    job_dir: Path,
    research_question: str,
) -> _ReportOutcome:
    """Run final report generation iteration and output artifact handling."""
    ks = KnowledgeState.load(job_dir / KS_FILENAME)
    report_prompt = build_report_prompt(research_question, ks)
    logger.info("Report generation iteration (prompt: %d chars)", len(report_prompt))
    report_result = await executor.run_iteration(report_prompt, reset_session=True)

    _save_report_transcript(job_dir, report_result.transcript)
    report_path = job_dir / "final_report.md"
    report_success = _ensure_report_written(report_path, report_result)

    if report_success:
        try:
            _try_generate_report_pdf(report_path)
        except (ValueError, OSError, ShandyError) as exc:
            logger.warning("PDF generation failed: %s", exc)

    return _ReportOutcome(success=report_success, error=report_result.error)


async def _persist_final_status(
    job_dir: Path,
    report_outcome: _ReportOutcome,
) -> str:
    """Persist final job status based on report generation outcome."""
    final_status = "completed" if report_outcome.success else "failed"
    if final_status == "completed":
        await update_job_status(job_dir, "completed")
    else:
        await update_job_status(
            job_dir,
            "failed",
            error_message=f"Report generation failed: {report_outcome.error}",
        )
    return final_status


async def _get_job_owner_id(job_id: str) -> UUID | None:
    """Load job owner id from the database."""
    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            result = await session.execute(
                select(JobModel.owner_id).where(JobModel.id == UUID(job_id))
            )
            return result.scalar_one_or_none()
    except Exception as e:
        logger.warning("Failed to load owner_id for job %s: %s", job_id, e)
        return None


async def _load_runtime_context(job_dir: Path) -> dict[str, Any]:
    """Load runtime job metadata from the database."""
    job_uuid = UUID(job_dir.name)

    async with AsyncSessionLocal(thread_safe=True) as session:
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
        "research_question": job.title,
        "max_iterations": job.max_iterations,
        "use_hypotheses": bool(job.use_hypotheses),
        "investigation_mode": job.investigation_mode,
        "data_files": resolved_files,
    }


async def _write_skills_to_claude_dir(job_dir: Path) -> None:
    """Write CLAUDE.md and enabled skill files into job_dir/.claude/."""
    claude_dir = job_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Always write the chat-agent CLAUDE.md so the chat agent finds it via cwd
    _write_chat_claude_md(claude_dir)

    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            skills = await get_enabled_skills(session)
        if not skills:
            logger.info("No enabled skills to write")
            return
        skills_dir = claude_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        for skill in skills:
            filename = f"{skill.category}--{skill.slug}.md"
            path = skills_dir / filename
            header = f"# {skill.name}\n*Category: {skill.category}*\n"
            if skill.description:
                header += f"\n{skill.description}\n"
            path.write_text(header + "\n" + skill.content, encoding="utf-8")
        logger.info("Wrote %d skill files to %s", len(skills), skills_dir)
    except Exception as e:
        logger.warning("Failed to write skills to .claude dir: %s", e)


def _write_chat_claude_md(claude_dir: Path) -> None:
    """Write CHAT_CLAUDE.md content to claude_dir/CLAUDE.md (chat agent entry point)."""
    try:
        src = Path(__file__).parent.parent.parent.parent / "CHAT_CLAUDE.md"
        dest = claude_dir / "CLAUDE.md"
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        logger.debug("Wrote chat CLAUDE.md to %s", dest)
    except Exception as e:
        logger.warning("Failed to write chat CLAUDE.md: %s", e)


def sync_knowledge_state_to_db(job_dir: Path, ks: KnowledgeState | None = None) -> None:
    """Sync knowledge state to database (non-blocking background task)."""
    try:
        job_id = job_dir.name
        if ks is None:
            ks_path = job_dir / KS_FILENAME
            if ks_path.exists():
                ks = KnowledgeState.load(ks_path)
            else:
                logger.warning("Knowledge state file not found: %s", ks_path)
                return

        async def _save_with_error_handling() -> None:
            try:
                owner_id = await _get_job_owner_id(job_id)
                await ks.save_to_database(job_id, owner_id)
            except Exception as e:
                logger.warning("Background database sync failed for job %s: %s", job_id, e)

        try:
            asyncio.get_running_loop()
            create_background_task(
                _save_with_error_handling(),
                name=f"sync-knowledge-state-{job_id}",
                logger=logger,
            )
        except RuntimeError:
            asyncio.run(_save_with_error_handling())

        logger.debug("Synced knowledge state to database for job %s", job_id)
    except Exception as e:
        logger.warning("Failed to sync knowledge state to database: %s", e)


def get_version_metadata() -> dict[str, str]:
    """Get SHANDY version metadata for reproducibility."""
    import os

    from shandy.version import SHORT_COMMIT_LENGTH, get_commit

    metadata: dict[str, str] = {}

    commit = get_commit()
    if commit != "unknown":
        metadata["shandy_commit"] = commit

    shandy_build_time = os.environ.get("SHANDY_BUILD_TIME")  # env-ok
    if shandy_build_time and shandy_build_time != "unknown":
        metadata["shandy_build_time"] = shandy_build_time

    try:
        if Path("/.dockerenv").exists():
            with open("/etc/hostname", encoding="utf-8") as f:
                container_id = f.read().strip()
                if container_id:
                    metadata["docker_container_id"] = container_id[:SHORT_COMMIT_LENGTH]
    except OSError:
        pass

    return metadata


_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "Anthropic": "claude-sonnet-4-20250514",
    "CBORG": "claude-sonnet-4-20250514",
    "Vertex AI": "claude-sonnet-4-5@20250929",
    "AWS Bedrock": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "Azure AI Foundry": "claude-sonnet-4-5",
}


async def _persist_job_cost_record(
    job_id: str,
    tokens: TokenUsage,
    provider_name: str,
    model_name: str,
    operation_type: str = "discovery",
) -> None:
    """Write a CostRecord for the completed job execution."""
    from shandy.database.models import CostRecord
    from shandy.providers.pricing import estimate_cost_usd

    cost_usd = estimate_cost_usd(model_name, tokens.input_tokens, tokens.output_tokens)
    async with AsyncSessionLocal(thread_safe=True) as session:
        record = CostRecord(
            job_id=UUID(job_id),
            iteration=None,
            operation_type=operation_type,
            provider=provider_name,
            model=model_name,
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cost_usd=cost_usd,
        )
        session.add(record)
        await session.commit()


async def run_discovery_async(job_dir: Path) -> dict[str, Any]:
    """
    Run autonomous discovery using the configured agent executor.

    This is an async entry point that JobManager (or the container entrypoint)
    calls.  The executor is chosen by agent.factory.get_agent_executor() based
    on the configured provider.

    Args:
        job_dir: Path to job directory

    Returns:
        Dict: {job_id, status, iterations, findings}
    """
    job_dir = Path(job_dir)
    runtime = await _load_runtime_context(job_dir)
    job_id = runtime["job_id"]
    logger.info("Starting discovery for job %s (mode=%s)", job_id, runtime["investigation_mode"])

    provider = get_provider()
    provider.setup_environment()
    await update_job_status(job_dir, "running")

    use_hypotheses = runtime["use_hypotheses"]
    all_data_files = [Path(p) for p in runtime["data_files"]]
    await _write_skills_to_claude_dir(job_dir)
    executor = _build_agent_executor(
        job_dir=job_dir,
        data_file=_resolve_primary_data_file(runtime["data_files"]),
        use_hypotheses=use_hypotheses,
        data_files=all_data_files,
    )
    logger.info("Created agent executor for job %s", job_id)

    ks_path = job_dir / KS_FILENAME
    provenance_dir = job_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    log_file = job_dir / "claude_iterations.log"

    try:
        await _run_primary_discovery_loop(
            executor=executor,
            job_dir=job_dir,
            runtime=runtime,
            ks_path=ks_path,
            provenance_dir=provenance_dir,
            log_file=log_file,
        )
        report_outcome = await _run_report_generation_phase(
            executor=executor,
            job_dir=job_dir,
            research_question=runtime["research_question"],
        )
        final_status = await _persist_final_status(job_dir, report_outcome)
        ks = KnowledgeState.load(ks_path)
        sync_knowledge_state_to_db(job_dir, ks)
        return {
            "job_id": job_id,
            "status": final_status,
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }

    except _DiscoveryCancelledError:
        logger.info("Discovery cancelled for job %s", job_id)
        ks = KnowledgeState.load(ks_path)
        sync_knowledge_state_to_db(job_dir, ks)
        return {
            "job_id": job_id,
            "status": "cancelled",
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }

    except Exception as e:
        logger.error("Discovery failed [%s]: %s", get_version_string(), e, exc_info=True)
        try:
            await update_job_status(job_dir, "failed", error_message=str(e))
        except Exception as status_error:
            logger.warning("Failed to persist failure status for job %s: %s", job_id, status_error)
        try:
            ks = KnowledgeState.load(ks_path)
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
        tokens = executor.total_tokens
        logger.info(
            "Agent executor completed: %d input tokens, %d output tokens",
            tokens.input_tokens,
            tokens.output_tokens,
        )
        try:
            settings = get_settings()
            model_name = (
                settings.provider.anthropic_model
                or settings.provider.anthropic_default_sonnet_model
                or _PROVIDER_DEFAULT_MODELS.get(provider.name, "unknown")
            )
            await _persist_job_cost_record(job_id, tokens, provider.name, model_name)
        except Exception as cost_err:
            logger.warning("Failed to persist cost record for job %s: %s", job_id, cost_err)
        await executor.shutdown()


async def regenerate_report_async(job_dir: Path) -> dict[str, Any]:
    """
    Re-run only the report generation phase for a completed/failed job.

    Loads the existing KnowledgeState and DB-backed job metadata, creates a fresh executor
    session, and generates a new final_report.md + PDF.

    Args:
        job_dir: Path to job directory (must contain knowledge_state.json)

    Returns:
        Dict: {job_id, status, report_success}
    """
    job_dir = Path(job_dir)
    runtime = await _load_runtime_context(job_dir)
    job_id = runtime["job_id"]

    logger.info("Regenerating report for job %s", job_id)

    # Set up provider and executor
    provider = get_provider()
    provider.setup_environment()
    await update_job_status(job_dir, "generating_report")

    use_hypotheses = runtime["use_hypotheses"]
    all_data_files = [Path(p) for p in runtime["data_files"]]
    await _write_skills_to_claude_dir(job_dir)
    executor = _build_agent_executor(
        job_dir=job_dir,
        data_file=_resolve_primary_data_file(runtime["data_files"]),
        use_hypotheses=use_hypotheses,
        data_files=all_data_files,
    )

    try:
        report_outcome = await _run_report_generation_phase(
            executor=executor,
            job_dir=job_dir,
            research_question=runtime["research_question"],
        )
        final_status = await _persist_final_status(job_dir, report_outcome)
        ks = KnowledgeState.load(job_dir / KS_FILENAME)
        sync_knowledge_state_to_db(job_dir, ks)

        return {
            "job_id": job_id,
            "status": final_status,
            "report_success": report_outcome.success,
        }

    except Exception as e:
        logger.error("Report regeneration failed [%s]: %s", get_version_string(), e, exc_info=True)
        try:
            await update_job_status(job_dir, "failed", error_message=str(e))
        except Exception as status_error:
            logger.warning(
                "Failed to persist regenerate-report failure for %s: %s", job_id, status_error
            )
        raise

    finally:
        tokens = executor.total_tokens
        logger.info(
            "Report regeneration executor: %d input tokens, %d output tokens",
            tokens.input_tokens,
            tokens.output_tokens,
        )
        try:
            settings = get_settings()
            model_name = (
                settings.provider.anthropic_model
                or settings.provider.anthropic_default_sonnet_model
                or _PROVIDER_DEFAULT_MODELS.get(provider.name, "unknown")
            )
            await _persist_job_cost_record(job_id, tokens, provider.name, model_name, "report")
        except Exception as cost_err:
            logger.warning("Failed to persist cost record for job %s: %s", job_id, cost_err)
        await executor.shutdown()


def _save_transcript(path: Path, transcript: list[dict[str, Any]]) -> None:
    """Save iteration transcript to JSON file."""
    import json as _json

    with open(path, "w", encoding="utf-8") as f:
        _json.dump(transcript, f, indent=2)
    logger.info("Saved transcript to %s", path)


def _append_log(
    log_file: Path,
    iteration: int,
    prompt: str,
    output: str,
    tool_calls: int,
    write: bool = False,
) -> None:
    """Append iteration summary to the log file."""
    mode = "w" if write else "a"
    with open(log_file, mode, encoding="utf-8") as f:
        f.write(f"=== Iteration {iteration} ===\n")
        f.write(f"Prompt: {prompt}\n\n")
        f.write(f"Output: {output}\n\n")
        f.write(f"Tool calls: {tool_calls}\n\n")
