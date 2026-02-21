"""
Async discovery loop for SHANDY autonomous research.

The public entry point is run_discovery_async(), which the JobManager thread
calls via asyncio.run().
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from shandy.agent.factory import get_agent_executor
from shandy.database.models import JobDataFile
from shandy.database.session import AsyncSessionLocal
from shandy.exceptions import ShandyError
from shandy.file_loader import get_file_info
from shandy.knowledge_state import KnowledgeState
from shandy.orchestrator.iteration import (
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
from shandy.version import get_version_string

logger = logging.getLogger(__name__)


ALLOWED_TOOLS = [
    "execute_code",
    "search_pubmed",
    "update_knowledge_state",
    "save_iteration_summary",
    "set_status",
    "set_job_title",
    "set_consensus_answer",
    "read_document",
    "add_hypothesis",
    "update_hypothesis",
    "run_phenix_tool",
    "compare_structures",
    "parse_alphafold_confidence",
]


async def _write_skills_to_claude_dir(job_dir: Path, use_skills: bool) -> None:
    """Write CLAUDE.md and enabled skill files into job_dir/.claude/."""
    claude_dir = job_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Always write the chat-agent CLAUDE.md so the chat agent finds it via cwd
    _write_chat_claude_md(claude_dir)

    if not use_skills:
        return
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
            ks_path = job_dir / "knowledge_state.json"
            if ks_path.exists():
                ks = KnowledgeState.load(ks_path)
            else:
                logger.warning("Knowledge state file not found: %s", ks_path)
                return

        config_path = job_dir / "config.json"
        owner_id = None
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
            owner_id_str = config.get("owner_id")
            if owner_id_str:
                owner_id = UUID(owner_id_str)

        try:
            loop = asyncio.get_running_loop()

            async def _save_with_error_handling() -> None:
                try:
                    await ks.save_to_database(job_id, owner_id)
                except Exception as e:
                    logger.warning("Background database sync failed for job %s: %s", job_id, e)

            loop.create_task(_save_with_error_handling())
        except RuntimeError:
            ks.save_to_database_sync(job_id, owner_id)

        logger.debug("Synced knowledge state to database for job %s", job_id)
    except Exception as e:
        logger.warning("Failed to sync knowledge state to database: %s", e)


def _persist_data_files_to_db(
    job_id: str,
    job_dir: Path,
    data_paths: list[Path],
    owner_id: UUID | None = None,
) -> None:
    """Persist uploaded data files to job_data_files table."""
    try:

        async def _save_data_files() -> None:
            async with AsyncSessionLocal(thread_safe=True) as session:
                for data_path in data_paths:
                    file_info = get_file_info(data_path)
                    relative_path = f"data/{data_path.name}"
                    data_file = JobDataFile(
                        job_id=UUID(job_id),
                        filename=data_path.name,
                        file_path=relative_path,
                        file_type=file_info["file_type"],
                        file_size=file_info["size"],
                        mime_type=file_info["mime_type"],
                    )
                    session.add(data_file)
                await session.commit()
                logger.info(
                    "Persisted %d data files to database for job %s",
                    len(data_paths),
                    job_id,
                )

        try:
            loop = asyncio.get_running_loop()

            async def _save_with_error_handling() -> None:
                try:
                    await _save_data_files()
                except Exception as e:
                    logger.warning("Background data file persist failed for job %s: %s", job_id, e)

            loop.create_task(_save_with_error_handling())
        except RuntimeError:
            asyncio.run(_save_data_files())

    except Exception as e:
        logger.warning("Failed to persist data files to database: %s", e)


def get_version_metadata() -> dict[str, str]:
    """Get SHANDY version metadata for reproducibility."""
    import os
    import subprocess

    metadata: dict[str, str] = {}

    shandy_commit = os.environ.get("SHANDY_COMMIT")  # noqa: env-ok
    if shandy_commit and shandy_commit != "unknown":
        metadata["shandy_commit"] = shandy_commit[:12]

    shandy_build_time = os.environ.get("SHANDY_BUILD_TIME")  # noqa: env-ok
    if shandy_build_time and shandy_build_time != "unknown":
        metadata["shandy_build_time"] = shandy_build_time

    if "shandy_commit" not in metadata:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                metadata["shandy_commit"] = result.stdout.strip()[:12]
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        if Path("/.dockerenv").exists():
            with open("/etc/hostname", encoding="utf-8") as f:
                container_id = f.read().strip()
                if container_id:
                    metadata["docker_container_id"] = container_id[:12]
    except OSError:
        pass

    return metadata


async def run_discovery_async(job_dir: Path) -> dict[str, Any]:
    """
    Run autonomous discovery using the configured agent executor.

    This is an async entry point that JobManager (or the container entrypoint)
    calls.  The executor is chosen by agent.factory.get_agent_executor() based
    on the configured provider.

    Args:
        job_dir: Path to job directory (must contain config.json)

    Returns:
        Dict: {job_id, status, iterations, findings}
    """
    job_dir = Path(job_dir)

    with open(job_dir / "config.json", encoding="utf-8") as f:
        config = json.load(f)

    job_id = config["job_id"]
    max_iterations = config["max_iterations"]
    investigation_mode = config.get("investigation_mode", "autonomous")
    data_file = Path(config["data_files"][0]) if config["data_files"] else None

    if data_file and not data_file.is_absolute():
        data_file = data_file.absolute()

    logger.info("Starting discovery for job %s (mode=%s)", job_id, investigation_mode)

    provider = get_provider()
    provider.setup_environment()

    config["status"] = "running"
    config["started_at"] = datetime.now(timezone.utc).isoformat()
    with open(job_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    use_skills = config.get("use_skills", True)
    await _write_skills_to_claude_dir(job_dir, use_skills)
    system_prompt = get_system_prompt(skills_enabled=use_skills)
    logger.info("Built system prompt (%d chars)", len(system_prompt))

    executor = get_agent_executor(
        job_dir=job_dir,
        data_file=data_file,
        allowed_tools=ALLOWED_TOOLS,
        system_prompt=system_prompt,
    )
    logger.info("Created agent executor for job %s", job_id)

    try:
        ks = KnowledgeState.load(job_dir / "knowledge_state.json")
        initial_prompt = build_initial_prompt(
            config["research_question"], max_iterations, config["data_files"], ks
        )

        provenance_dir = job_dir / "provenance"
        provenance_dir.mkdir(parents=True, exist_ok=True)
        log_file = job_dir / "claude_iterations.log"

        logger.info("Iteration 1/%d: Starting session", max_iterations)
        result = await executor.run_iteration(initial_prompt, reset_session=True)

        if not result.success:
            logger.error("Iteration 1 failed: %s", result.error)
            raise RuntimeError(f"Agent loop failed: {result.error}")

        logger.info("Iteration 1 completed (tool_calls=%d)", result.tool_calls)

        version_info = get_version_metadata()
        if version_info:
            ks = KnowledgeState.load(job_dir / "knowledge_state.json")
            ks.set_version_info(version_info)
            ks.save(job_dir / "knowledge_state.json")
            sync_knowledge_state_to_db(job_dir, ks)

        _save_transcript(provenance_dir / "iter1_transcript.json", result.transcript)
        _append_log(log_file, 1, initial_prompt, result.output, result.tool_calls, write=True)

        ks_path = job_dir / "knowledge_state.json"
        if max_iterations > 1:
            increment_ks_iteration(ks_path)

        pending_feedback: str | None = None
        if investigation_mode == "coinvestigate" and max_iterations > 1:
            update_job_status(job_dir, "awaiting_feedback")
            pending_feedback = wait_for_feedback_or_timeout(job_dir)
            update_job_status(job_dir, "running")

        reset_interval = 5

        for iteration in range(2, max_iterations + 1):
            ks = KnowledgeState.load(job_dir / "knowledge_state.json")

            if pending_feedback is None:
                pending_feedback = ks.get_feedback_for_iteration(iteration)

            iteration_prompt = build_iteration_prompt(
                iteration, max_iterations, ks, pending_feedback
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
                break

            logger.info("Iteration %d completed (tool_calls=%d)", iteration, result.tool_calls)

            _save_transcript(provenance_dir / f"iter{iteration}_transcript.json", result.transcript)
            _append_log(log_file, iteration, iteration_prompt, result.output, result.tool_calls)

            if iteration < max_iterations:
                increment_ks_iteration(ks_path)

            if investigation_mode == "coinvestigate" and iteration < max_iterations:
                update_job_status(job_dir, "awaiting_feedback")
                pending_feedback = wait_for_feedback_or_timeout(job_dir)
                update_job_status(job_dir, "running")

        logger.info("Discovery loop completed")

        # Report generation iteration (extra, not counted toward max_iterations)
        ks = KnowledgeState.load(job_dir / "knowledge_state.json")
        report_prompt = build_report_prompt(config["research_question"], ks)

        logger.info("Report generation iteration (prompt: %d chars)", len(report_prompt))
        report_result = await executor.run_iteration(report_prompt, reset_session=True)

        _save_transcript(provenance_dir / "report_transcript.json", report_result.transcript)

        # Check if agent wrote final_report.md
        report_path = job_dir / "final_report.md"
        report_success = report_path.exists()

        if not report_success and report_result.success and len(report_result.output) > 500:
            # Fallback: agent returned report as output text instead of writing file
            report_path.write_text(report_result.output, encoding="utf-8")
            report_success = True

        if report_success:
            try:
                from shandy.pdf_generator import markdown_to_pdf

                markdown_to_pdf(report_path, add_footer=True)
            except (ValueError, OSError, ShandyError) as e:
                logger.warning("PDF generation failed: %s", e)

        ks = KnowledgeState.load(job_dir / "knowledge_state.json")

        if report_success:
            config["status"] = "completed"
        else:
            config["status"] = "failed"
            config["error"] = f"Report generation failed: {report_result.error}"

        config["completed_at"] = datetime.now(timezone.utc).isoformat()
        config["iterations_completed"] = ks.data["iteration"]
        config["findings_count"] = len(ks.data["findings"])
        config["max_iterations"] = ks.data["iteration"]

        with open(job_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        sync_knowledge_state_to_db(job_dir, ks)

        return {
            "job_id": job_id,
            "status": config["status"],
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }

    except Exception as e:  # noqa: BLE001
        logger.error("Discovery failed [%s]: %s", get_version_string(), e, exc_info=True)
        config["status"] = "failed"
        config["error"] = str(e)
        config["failed_at"] = datetime.now(timezone.utc).isoformat()
        with open(job_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        raise

    finally:
        tokens = executor.total_tokens
        logger.info(
            "Agent executor completed: %d input tokens, %d output tokens",
            tokens.input_tokens,
            tokens.output_tokens,
        )
        await executor.shutdown()


async def regenerate_report_async(job_dir: Path) -> dict[str, Any]:
    """
    Re-run only the report generation phase for a completed/failed job.

    Loads the existing KnowledgeState and config, creates a fresh executor
    session, and generates a new final_report.md + PDF.

    Args:
        job_dir: Path to job directory (must contain config.json and knowledge_state.json)

    Returns:
        Dict: {job_id, status, report_success}
    """
    job_dir = Path(job_dir)

    with open(job_dir / "config.json", encoding="utf-8") as f:
        config = json.load(f)

    job_id = config["job_id"]

    logger.info("Regenerating report for job %s", job_id)

    # Set up provider and executor
    provider = get_provider()
    provider.setup_environment()

    use_skills = config.get("use_skills", True)
    await _write_skills_to_claude_dir(job_dir, use_skills)
    system_prompt = get_system_prompt(skills_enabled=use_skills)

    data_file = Path(config["data_files"][0]) if config["data_files"] else None
    if data_file and not data_file.is_absolute():
        data_file = data_file.absolute()

    executor = get_agent_executor(
        job_dir=job_dir,
        data_file=data_file,
        allowed_tools=ALLOWED_TOOLS,
        system_prompt=system_prompt,
    )

    try:
        ks = KnowledgeState.load(job_dir / "knowledge_state.json")
        report_prompt = build_report_prompt(config["research_question"], ks)

        logger.info("Report generation iteration (prompt: %d chars)", len(report_prompt))
        report_result = await executor.run_iteration(report_prompt, reset_session=True)

        provenance_dir = job_dir / "provenance"
        provenance_dir.mkdir(parents=True, exist_ok=True)
        _save_transcript(provenance_dir / "report_transcript.json", report_result.transcript)

        # Check if agent wrote final_report.md
        report_path = job_dir / "final_report.md"
        report_success = report_path.exists()

        if not report_success and report_result.success and len(report_result.output) > 500:
            # Fallback: agent returned report as output text instead of writing file
            report_path.write_text(report_result.output, encoding="utf-8")
            report_success = True

        if report_success:
            try:
                from shandy.pdf_generator import markdown_to_pdf

                markdown_to_pdf(report_path, add_footer=True)
            except (ValueError, OSError, ShandyError) as e:
                logger.warning("PDF generation failed: %s", e)

        ks = KnowledgeState.load(job_dir / "knowledge_state.json")

        if report_success:
            config["status"] = "completed"
        else:
            config["status"] = "failed"
            config["error"] = f"Report generation failed: {report_result.error}"

        config["completed_at"] = datetime.now(timezone.utc).isoformat()

        with open(job_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        sync_knowledge_state_to_db(job_dir, ks)

        return {
            "job_id": job_id,
            "status": config["status"],
            "report_success": report_success,
        }

    except Exception as e:  # noqa: BLE001
        logger.error("Report regeneration failed [%s]: %s", get_version_string(), e, exc_info=True)
        config["status"] = "failed"
        config["error"] = str(e)
        config["failed_at"] = datetime.now(timezone.utc).isoformat()
        with open(job_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        raise

    finally:
        tokens = executor.total_tokens
        logger.info(
            "Report regeneration executor: %d input tokens, %d output tokens",
            tokens.input_tokens,
            tokens.output_tokens,
        )
        await executor.shutdown()


def _save_transcript(path: Path, transcript: list[dict]) -> None:
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
