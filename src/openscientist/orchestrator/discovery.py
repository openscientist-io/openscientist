"""
Async discovery loop for OpenScientist autonomous research.

The public entry point is run_discovery_async(), which the JobManager thread
calls via asyncio.run().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from openscientist.agent.base import AbstractAgent, AgentConfig, IterationResult, TokenUsage
from openscientist.agent.factory import get_agent
from openscientist.database.models import JobDataFile
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import AsyncSessionLocal
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
from openscientist.prompts import (
    AgentBackend,
    generate_job_agents_md,
    generate_job_claude_md,
    get_enabled_skills,
    get_system_prompt,
)
from openscientist.providers import get_provider
from openscientist.providers.base import ClaudeCompatible, Provider
from openscientist.settings import get_settings
from openscientist.transcript import TranscriptEntry, save_transcript
from openscientist.version import get_version_string

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


def _backend_for(provider: Provider) -> AgentBackend:
    """Map a provider to its agent backend name (for prompt selection)."""
    return "claude_code" if isinstance(provider, ClaudeCompatible) else "codex"


def _build_agent_executor(
    job_dir: Path,
    data_file: Path | None,
    *,
    agent_backend: AgentBackend = "claude_code",
    use_hypotheses: bool = False,
    data_files: list[Path] | None = None,
) -> AbstractAgent[Provider]:
    """Create a configured agent for discovery/report phases.

    Claude gets a concise system prompt (its rich ``CLAUDE.md`` is written
    separately into ``.claude/``). Codex reads a single ``AGENTS.md``, so
    its system prompt is the full per-job doc, written there by the agent.
    """
    if agent_backend == "codex":
        system_prompt = generate_job_agents_md(
            use_hypotheses=use_hypotheses,
            phenix_available=get_settings().phenix.is_available,
        )
    else:
        system_prompt = get_system_prompt(agent_backend="claude_code")
    logger.info("Built %s system prompt (%d chars)", agent_backend, len(system_prompt))
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
    )


def _sync_version_metadata_if_available(job_id: str) -> None:
    """Store runtime version metadata in knowledge state when available."""
    version_info = get_version_metadata()
    if not version_info:
        return
    ks = KnowledgeState.load_from_database_sync(job_id)
    ks.set_version_info(version_info)
    ks.save_to_database_sync(job_id)


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

    ks = KnowledgeState.load_from_database_sync(job_id)
    initial_prompt = build_initial_prompt(
        runtime["research_question"],
        max_iterations,
        data_files,
        ks,
        description=runtime.get("description"),
    )

    logger.info("Iteration 1/%d: Starting session", max_iterations)
    result = await executor.run_iteration(initial_prompt, reset_session=True)
    if not result.success:
        logger.error("Iteration 1 failed: %s", result.error)
        raise RuntimeError(f"Agent loop failed: {result.error}")
    logger.info("Iteration 1 completed (tool_calls=%d)", result.tool_calls)

    _sync_version_metadata_if_available(job_id)
    _append_iteration_artifacts(
        provenance_dir=provenance_dir,
        log_file=log_file,
        iteration=1,
        prompt=initial_prompt,
        result=result,
        overwrite_log=True,
    )
    if max_iterations > 1:
        increment_ks_iteration(job_id)
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
        ks = KnowledgeState.load_from_database_sync(job_id)
        if pending_feedback is None:
            pending_feedback = ks.get_feedback_for_iteration(iteration)

        iteration_prompt = build_iteration_prompt(
            iteration,
            max_iterations,
            ks,
            pending_feedback,
            description=runtime.get("description"),
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
            increment_ks_iteration(job_id)
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


def _ensure_report_written(report_path: Path, report_result: IterationResult) -> bool:
    """Ensure final_report.md exists at the expected location after the report iteration.

    If the agent wrote the file to a subdirectory within the job dir, move it
    to the expected path.  Returns False when the report cannot be found —
    the caller marks the job as failed.
    """
    if report_path.exists():
        return True

    # Check if the agent nested the file within the job directory.
    job_dir = report_path.parent
    for found in job_dir.rglob("final_report.md"):
        if found != report_path:
            logger.warning("Report found at %s — moving to %s", found, report_path)
            found.rename(report_path)
            return True

    logger.error(
        "Report file not found at %s after report iteration (agent output: %.200s)",
        report_path,
        report_result.output,
    )
    return False


async def _try_generate_report_pdf(report_path: Path) -> None:
    """Generate HTML and PDF from markdown report.

    Pipeline: markdown (with figure tags) → HTML → PDF (via WeasyPrint).
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

        # Render PDF from HTML
        await render_report_pdf(html_path, pdf_path, job_dir)
        return

    except Exception as exc:
        logger.warning("WeasyPrint PDF generation failed, falling back to fpdf2: %s", exc)

    # Fallback: strip figure tags and use fpdf2
    try:
        from openscientist.pdf_generator import markdown_to_pdf
        from openscientist.report.processor import strip_figure_tags

        raw_md = report_path.read_text(encoding="utf-8")
        stripped = strip_figure_tags(raw_md)
        # Write stripped version to a temp path for fpdf2
        stripped_path = job_dir / "_final_report_stripped.md"
        stripped_path.write_text(stripped, encoding="utf-8")
        try:
            markdown_to_pdf(stripped_path, pdf_path, add_footer=True)
        finally:
            stripped_path.unlink(missing_ok=True)
    except Exception as fallback_exc:
        logger.warning("fpdf2 fallback also failed: %s", fallback_exc)


# A weak model sometimes ends a turn without actually producing the deliverable
# (e.g. it describes the report instead of writing the file). Re-ask in the same
# session a bounded number of times. The model still authors it, and the job
# fails honestly if the attempts are exhausted.
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

    The first attempt sends the full report prompt in a fresh session. Later
    attempts send a short focused reminder in the same session. Returns the last
    turn result and whether the report file now exists.
    """
    report_path = job_dir / "final_report.md"
    prompt = build_report_prompt(research_question, ks, job_dir=job_dir, description=description)
    logger.info("Report generation turn (prompt: %d chars)", len(prompt))

    result = await executor.run_iteration(prompt, reset_session=True)
    for attempt in range(1, _MAX_REPORT_ATTEMPTS + 1):
        if _ensure_report_written(report_path, result):
            if attempt > 1:
                logger.info("Report written on attempt %d", attempt)
            return result, True
        if attempt == _MAX_REPORT_ATTEMPTS:
            break
        logger.warning(
            "Report file missing after attempt %d/%d; re-asking", attempt, _MAX_REPORT_ATTEMPTS
        )
        result = await executor.run_iteration(
            build_report_retry_prompt(str(report_path)), reset_session=False
        )
    logger.error("Report file not written after %d attempts", _MAX_REPORT_ATTEMPTS)
    return result, False


async def _set_consensus_answer(
    executor: AbstractAgent[Provider], job_dir: Path, research_question: str
) -> None:
    """Run the consensus turn, re-asking until the model records an answer.

    The model writes the consensus itself. This only re-prompts. If the attempts
    are exhausted the report still stands and the job completes without a
    consensus (logged), rather than fabricating one.
    """
    for attempt in range(1, _MAX_CONSENSUS_ATTEMPTS + 1):
        prompt = (
            build_consensus_prompt(research_question)
            if attempt == 1
            else build_consensus_retry_prompt(research_question)
        )
        await executor.run_iteration(prompt, reset_session=False)
        if KnowledgeState.load_from_database_sync(job_dir.name).data.get("consensus_answer"):
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


async def _run_report_generation_phase(
    executor: AbstractAgent[Provider],
    job_dir: Path,
    research_question: str,
    description: str | None = None,
) -> _ReportOutcome:
    """Run the report and consensus turns (each with bounded retries) and output
    artifact handling."""
    ks = KnowledgeState.load_from_database_sync(job_dir.name)
    report_result, report_success = await _run_report_turn(
        executor, job_dir, research_question, ks, description
    )
    _save_report_transcript(job_dir, report_result.transcript)
    report_path = job_dir / "final_report.md"

    if report_success:
        # Dedicated consensus turn (separate from the report so a weaker model
        # commits fully to one deliverable at a time). The model writes it.
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
        await update_job_status(job_dir, "completed")
    else:
        await update_job_status(
            job_dir,
            "failed",
            error_message=f"Report generation failed: {report_outcome.error}",
        )
    return final_status


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
        "research_question": job.research_question,
        "description": getattr(job, "description", None),
        "max_iterations": job.max_iterations,
        "use_hypotheses": bool(job.use_hypotheses),
        "investigation_mode": job.investigation_mode,
        "data_files": resolved_files,
    }


async def _write_skills_to_claude_dir(job_dir: Path, *, use_hypotheses: bool = False) -> None:
    """Write CLAUDE.md and enabled skill files into job_dir/.claude/."""
    claude_dir = job_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Write the discovery-agent JOB_CLAUDE.md (hypothesis sections conditional)
    _write_job_claude_md(claude_dir, use_hypotheses=use_hypotheses)

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
        dest = claude_dir / "CLAUDE.md"
        dest.write_text(_read_chat_claude_md_template(), encoding="utf-8")
        logger.debug("Wrote chat CLAUDE.md to %s", dest)
    except Exception as e:
        logger.warning("Failed to write chat CLAUDE.md: %s", e)


def _read_chat_claude_md_template() -> str:
    """Read the packaged CHAT_CLAUDE.md template used by job chat."""
    return (
        resources.files("openscientist.templates")
        .joinpath("CHAT_CLAUDE.md")
        .read_text(encoding="utf-8")
    )


def _write_job_claude_md(claude_dir: Path, *, use_hypotheses: bool = False) -> None:
    """Write generated JOB_CLAUDE.md content to claude_dir/CLAUDE.md."""
    from openscientist.settings import get_settings

    try:
        phenix_available = get_settings().phenix.is_available
        dest = claude_dir / "CLAUDE.md"
        dest.write_text(
            generate_job_claude_md(
                use_hypotheses=use_hypotheses, phenix_available=phenix_available
            ),
            encoding="utf-8",
        )
        logger.debug("Wrote job CLAUDE.md to %s (use_hypotheses=%s)", dest, use_hypotheses)
    except Exception as e:
        logger.warning("Failed to write job CLAUDE.md: %s", e)


def get_version_metadata() -> dict[str, str]:
    """Get OpenScientist version metadata for reproducibility."""
    import os

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
    from openscientist.database.models import CostRecord
    from openscientist.providers.pricing import estimate_cost_usd

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
    calls.  The agent is chosen by agent.factory.get_agent() based
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
    # setup_environment() is a Claude-family concern (env/routing flags).
    # Codex providers configure the child via the agent's config.toml.
    if isinstance(provider, ClaudeCompatible):
        provider.setup_environment()
    backend = _backend_for(provider)
    await update_job_status(job_dir, "running")

    use_hypotheses = runtime["use_hypotheses"]
    all_data_files = [Path(p) for p in runtime["data_files"]]
    # The .claude/ skills + CLAUDE.md are Claude-only. The Codex agent reads
    # its instructions from the AGENTS.md it writes from the system prompt.
    if backend == "claude_code":
        await _write_skills_to_claude_dir(job_dir, use_hypotheses=use_hypotheses)
    executor = _build_agent_executor(
        job_dir=job_dir,
        data_file=_resolve_primary_data_file(runtime["data_files"]),
        agent_backend=backend,
        use_hypotheses=use_hypotheses,
        data_files=all_data_files,
    )
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
        )
        report_outcome = await _run_report_generation_phase(
            executor=executor,
            job_dir=job_dir,
            research_question=runtime["research_question"],
            description=runtime.get("description"),
        )
        final_status = await _persist_final_status(job_dir, report_outcome)
        ks = KnowledgeState.load_from_database_sync(job_id)
        return {
            "job_id": job_id,
            "status": final_status,
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }

    except _DiscoveryCancelledError:
        logger.info("Discovery cancelled for job %s", job_id)
        ks = KnowledgeState.load_from_database_sync(job_id)
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
            ks = KnowledgeState.load_from_database_sync(job_id)
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
                settings.provider.model
                or settings.provider.anthropic_default_sonnet_model
                or _PROVIDER_DEFAULT_MODELS.get(provider.display_name, "unknown")
            )
            await _persist_job_cost_record(job_id, tokens, provider.display_name, model_name)
        except Exception as cost_err:
            logger.warning("Failed to persist cost record for job %s: %s", job_id, cost_err)
        await executor.shutdown()


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
) -> None:
    """Append iteration summary to the log file."""
    mode = "w" if write else "a"
    with open(log_file, mode, encoding="utf-8") as f:
        f.write(f"=== Iteration {iteration} ===\n")
        f.write(f"Prompt: {prompt}\n\n")
        f.write(f"Output: {output}\n\n")
        f.write(f"Tool calls: {tool_calls}\n\n")
