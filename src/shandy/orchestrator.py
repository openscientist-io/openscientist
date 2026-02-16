"""
Orchestrator for SHANDY autonomous discovery.

Uses the Anthropic SDK with MCP tools for autonomous discovery.
"""

import asyncio
import fcntl
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import UUID

from dotenv import load_dotenv

from shandy.agent_loop import create_agent_loop
from shandy.database.models import JobDataFile
from shandy.exceptions import ShandyError
from shandy.file_loader import get_file_info
from shandy.knowledge_state import KnowledgeState
from shandy.prompts import (
    format_skills_content,
    get_relevant_skills_with_scores,
    get_system_prompt,
)
from shandy.providers import get_provider

# Load environment variables
if not load_dotenv("/app/.env", override=True):
    load_dotenv(".env", override=True)

logger = logging.getLogger(__name__)


FEEDBACK_TIMEOUT_SECONDS = 15 * 60  # 15 minutes


def sync_knowledge_state_to_db(job_dir: Path, ks: Optional[KnowledgeState] = None) -> None:
    """
    Sync knowledge state to database.

    Args:
        job_dir: Job directory path
        ks: KnowledgeState instance (if None, will load from file)
    """
    try:
        # Extract job_id from path
        job_id = job_dir.name

        # Load KS if not provided
        if ks is None:
            ks_path = job_dir / "knowledge_state.json"
            if ks_path.exists():
                ks = KnowledgeState.load(ks_path)
            else:
                logger.warning("Knowledge state file not found: %s", ks_path)
                return

        # Extract owner_id from config.json if available
        config_path = job_dir / "config.json"
        owner_id = None
        if config_path.exists():
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
                owner_id_str = config.get("owner_id")
                if owner_id_str:
                    owner_id = UUID(owner_id_str)

        # Check if we're in an async context (event loop already running)
        try:
            loop = asyncio.get_running_loop()

            # We're in an async context - schedule as a background task
            # Add error callback to prevent "Future exception was never retrieved"
            async def _save_with_error_handling():
                try:
                    await ks.save_to_database(job_id, owner_id)
                except Exception as e:
                    logger.warning("Background database sync failed for job %s: %s", job_id, e)

            loop.create_task(_save_with_error_handling())
        except RuntimeError:
            # No running loop - use sync wrapper (creates new loop)
            ks.save_to_database_sync(job_id, owner_id)

        logger.debug("Synced knowledge state to database for job %s", job_id)

    except Exception as e:
        # Log but don't fail - database sync is supplementary to file storage
        logger.warning("Failed to sync knowledge state to database: %s", e)


def _persist_data_files_to_db(
    job_id: str,
    job_dir: Path,
    data_paths: list[Path],
    owner_id: Optional[UUID] = None,
) -> None:
    """
    Persist uploaded data files to job_data_files table.

    Args:
        job_id: Job UUID string
        job_dir: Path to job directory
        data_paths: List of absolute paths to data files
        owner_id: Optional owner UUID for the job
    """
    try:
        from shandy.database.session import AsyncSessionLocal

        async def _save_data_files() -> None:
            async with AsyncSessionLocal(thread_safe=True) as session:
                for data_path in data_paths:
                    # Get file metadata
                    file_info = get_file_info(data_path)

                    # Store relative path (e.g., "data/filename.csv")
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

        # Check if we're in an async context
        try:
            loop = asyncio.get_running_loop()

            # We're in an async context - schedule as a background task
            async def _save_with_error_handling() -> None:
                try:
                    await _save_data_files()
                except Exception as e:
                    logger.warning("Background data file persist failed for job %s: %s", job_id, e)

            loop.create_task(_save_with_error_handling())
        except RuntimeError:
            # No running loop - run synchronously
            asyncio.run(_save_data_files())

    except Exception as e:
        # Log but don't fail job creation - database record is supplementary
        logger.warning("Failed to persist data files to database: %s", e)


def get_version_metadata() -> Dict[str, str]:
    """
    Get SHANDY version metadata for reproducibility.

    Returns:
        Dict with shandy_commit, shandy_build_time, and docker_container_id
    """
    metadata = {}

    # Try environment variables first (set during Docker build)
    shandy_commit = os.environ.get("SHANDY_COMMIT")  # noqa: env-ok
    if shandy_commit and shandy_commit != "unknown":
        metadata["shandy_commit"] = shandy_commit[:12]

    shandy_build_time = os.environ.get("SHANDY_BUILD_TIME")  # noqa: env-ok
    if shandy_build_time and shandy_build_time != "unknown":
        metadata["shandy_build_time"] = shandy_build_time

    # Fallback to git if not in Docker
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

    # Get docker container ID (if running in Docker)
    try:
        if Path("/.dockerenv").exists():
            # Read hostname which is the container ID in Docker
            with open("/etc/hostname", "r", encoding="utf-8") as f:
                container_id = f.read().strip()
                if container_id:
                    metadata["docker_container_id"] = container_id[:12]
    except OSError:
        pass

    return metadata


def wait_for_feedback_or_timeout(
    job_dir: Path, timeout_seconds: int = FEEDBACK_TIMEOUT_SECONDS
) -> Optional[str]:
    """
    Wait for scientist feedback or timeout (coinvestigate mode).

    Polls the knowledge graph for new feedback entries. Returns when:
    - Feedback is submitted (returns feedback text)
    - Timeout expires (returns None)
    - Job is cancelled (returns None)

    Args:
        job_dir: Path to job directory
        timeout_seconds: How long to wait before auto-continuing

    Returns:
        Feedback text if submitted, None if timeout or cancelled
    """

    config_path = job_dir / "config.json"
    ks_path = job_dir / "knowledge_state.json"

    start_time = time.time()
    last_feedback_count = 0

    # Get initial feedback count
    ks = KnowledgeState.load(ks_path)
    current_iteration = ks.data["iteration"]
    last_feedback_count = len(ks.data.get("feedback_history", []))

    logger.info("Waiting for scientist feedback (timeout: %ds)", timeout_seconds)

    while True:
        elapsed = time.time() - start_time

        # Check timeout
        if elapsed >= timeout_seconds:
            logger.info("Feedback timeout after %.0fs - auto-continuing", elapsed)
            return None

        # Check if job was cancelled
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        if config.get("status") == "cancelled":
            logger.info("Job cancelled while waiting for feedback")
            return None

        # Check for new feedback
        ks = KnowledgeState.load(ks_path)
        feedback_history = ks.data.get("feedback_history", [])

        if len(feedback_history) > last_feedback_count:
            # New feedback added - check if it's for current iteration
            latest = feedback_history[-1]
            if latest.get("after_iteration") == current_iteration:
                logger.info("Received feedback: %s...", latest["text"][:100])
                return str(latest["text"])

        # Check for continue signal (status changed back to running)
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        if config.get("status") == "running":
            logger.info("Continue signal received (no feedback)")
            return None

        # Sleep before next poll
        time.sleep(2)


def _send_iteration_notification(job_dir: Path, config: Dict[str, Any]) -> None:
    """Send ntfy notification when an iteration completes.

    Args:
        job_dir: Path to job directory
        config: Job configuration dictionary containing ntfy settings
    """
    if not config.get("ntfy_enabled") or not config.get("ntfy_topic"):
        return

    try:
        import httpx

        from shandy.settings import get_settings

        settings = get_settings()
        topic = config["ntfy_topic"]
        job_id = config["job_id"]

        # Use short_title if available, otherwise truncate research question
        short_title = config.get("short_title")
        if not short_title:
            research_q = config.get("research_question", "Unknown job")
            short_title = research_q[:50] + "..." if len(research_q) > 50 else research_q

        # Get current iteration from knowledge state
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

        # Use synchronous request (orchestrator runs in subprocess)
        with httpx.Client(timeout=10.0) as client:
            response = client.post(url, content=message, headers=headers)
            response.raise_for_status()
            logger.info("Sent iteration notification to topic %s", topic)
    except Exception as e:
        logger.warning("Failed to send ntfy notification: %s", e)


async def _load_skills_for_job(job_id: str, research_question: str, use_skills: bool) -> str:
    """
    Load relevant skills for a job's research question and persist to database.

    Uses full-text search to find skills matching the research question,
    stores them in the job_skills junction table, then formats them for
    injection into the system prompt.

    Args:
        job_id: The job's UUID string
        research_question: The job's research question
        use_skills: Whether skills are enabled for this job

    Returns:
        Formatted skills content string (empty if skills disabled or none found)
    """
    if not use_skills:
        return ""

    try:
        from shandy.database.models import JobSkill
        from shandy.database.session import AsyncSessionLocal

        # Use thread_safe=True since this is called via asyncio.run()
        # which creates a new event loop separate from the main app loop
        async with AsyncSessionLocal(thread_safe=True) as session:
            skills_with_scores = await get_relevant_skills_with_scores(
                session, research_question, limit=5
            )
            if skills_with_scores:
                skills = [s for s, _ in skills_with_scores]
                logger.info(
                    "Loaded %d skills for job: %s",
                    len(skills),
                    ", ".join(s.name for s in skills),
                )

                # Persist skills to job_skills table with source="initial"
                for skill, score in skills_with_scores:
                    job_skill = JobSkill(
                        job_id=UUID(job_id),
                        skill_id=skill.id,
                        skill_name=skill.name,
                        skill_category=skill.category,
                        skill_content=skill.content,
                        source="initial",
                        similarity_score=score,
                    )
                    session.add(job_skill)

                await session.commit()
                logger.info("Persisted %d initial skills for job %s", len(skills), job_id)

                return format_skills_content(skills)
            else:
                logger.info("No skills found for job")
                return ""
    except Exception as e:
        logger.warning("Failed to load skills: %s", e)
        return ""


def update_job_status(job_dir: Path, status: str) -> None:
    """Update job status in config.json and send ntfy notification if applicable."""
    config_path = job_dir / "config.json"
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    old_status = config.get("status")
    config["status"] = status

    # Track when we started awaiting feedback (for countdown timer)
    if status == "awaiting_feedback":
        config["awaiting_feedback_since"] = datetime.now().isoformat()
    elif "awaiting_feedback_since" in config:
        del config["awaiting_feedback_since"]

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # Send ntfy notification when iteration completes (entering awaiting_feedback)
    if status == "awaiting_feedback" and old_status != "awaiting_feedback":
        _send_iteration_notification(job_dir, config)


def increment_ks_iteration(ks_path: Path) -> None:
    """
    Safely increment the knowledge graph iteration counter with file locking.

    This ensures mutual exclusion with MCP server writes to prevent race conditions.

    Args:
        ks_path: Path to knowledge_state.json
    """
    with open(ks_path, "r+", encoding="utf-8") as f:
        # Acquire exclusive lock
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            # Read current state
            kg_data = json.load(f)

            # Increment iteration
            kg_data["iteration"] += 1

            # Write back atomically
            f.seek(0)
            f.truncate()
            json.dump(kg_data, f, indent=2)
        finally:
            # Release lock
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def create_job(
    job_id: str,
    research_question: str,
    data_files: list,
    max_iterations: int,
    use_skills: bool = True,
    jobs_dir: Path = Path("jobs"),
    investigation_mode: str = "autonomous",
    owner_id: Optional[str] = None,
    ntfy_enabled: bool = False,
    ntfy_topic: Optional[str] = None,
) -> Path:
    """
    Create a new discovery job.

    Args:
        job_id: Unique job identifier
        research_question: User's research question
        data_files: List of uploaded data file paths
        max_iterations: Maximum number of iterations
        use_skills: Whether to use skills
        jobs_dir: Base directory for jobs
        investigation_mode: "autonomous" (default) or "coinvestigate"
        owner_id: UUID string of the job owner (for notifications)
        ntfy_enabled: Whether ntfy notifications are enabled for the owner
        ntfy_topic: The ntfy topic for push notifications

    Returns:
        Path to job directory
    """
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (job_dir / "data").mkdir(exist_ok=True)
    (job_dir / "provenance").mkdir(exist_ok=True)

    # Copy data files to job directory, preserving original names/extensions (if any)
    data_paths = []
    if data_files:
        for data_file in data_files:
            # Preserve original filename
            original_name = Path(data_file).name
            dest = job_dir / "data" / original_name
            # In real implementation, handle file upload properly
            # For now, assume data_file is already a path
            import shutil

            shutil.copy(data_file, dest)
            data_paths.append(dest)

    # Persist data file records to database
    if data_paths:
        owner_uuid = UUID(owner_id) if owner_id else None
        _persist_data_files_to_db(job_id, job_dir, data_paths, owner_uuid)

    # Initialize knowledge graph
    ks = KnowledgeState(
        job_id=job_id,
        research_question=research_question,
        max_iterations=max_iterations,
        use_skills=use_skills,
    )

    # Add data summary - metadata only, no data loading!
    # This keeps job creation fast (no 30-40s wait for large files)
    # Data will be loaded on-demand by MCP server when first needed
    if data_paths:
        first_file = data_paths[0]

        # Get file type (fast - just checks extension and file size)
        file_info = get_file_info(first_file)

        # Set minimal data summary (agent will discover details when analyzing)
        ks.set_data_summary(
            {
                "files": [str(p.name) for p in data_paths],
                "file_type": file_info["file_type"],
                "file_size_mb": file_info["size"] / (1024 * 1024),
            }
        )
    else:
        # No data files provided
        ks.set_data_summary({"files": [], "file_type": "none", "file_size_mb": 0})

    ks.save(job_dir / "knowledge_state.json")
    sync_knowledge_state_to_db(job_dir, ks)  # Sync to database

    # Save job config
    config = {
        "job_id": job_id,
        "research_question": research_question,
        "data_files": [str(p) for p in data_paths],
        "max_iterations": max_iterations,
        "use_skills": use_skills,
        "investigation_mode": investigation_mode,
        "created_at": datetime.now().isoformat(),
        "status": "created",
        "owner_id": owner_id,
        "ntfy_enabled": ntfy_enabled,
        "ntfy_topic": ntfy_topic,
    }

    with open(job_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    logger.info("Created job %s at %s", job_id, job_dir)
    return job_dir


def run_discovery(job_dir: Path) -> Dict[str, Any]:
    """
    Run autonomous discovery using the Anthropic SDK agent loop.

    Args:
        job_dir: Path to job directory

    Returns:
        Dictionary with job results
    """
    job_dir = Path(job_dir)

    # Load job config
    with open(job_dir / "config.json", encoding="utf-8") as f:
        config = json.load(f)

    job_id = config["job_id"]
    max_iterations = config["max_iterations"]
    investigation_mode = config.get("investigation_mode", "autonomous")
    data_file = Path(config["data_files"][0]) if config["data_files"] else None

    logger.info("Investigation mode: %s", investigation_mode)

    # Ensure data_file is absolute (if present)
    if data_file and not data_file.is_absolute():
        data_file = data_file.absolute()

    logger.info("Starting discovery for job %s", job_id)

    # Initialize provider and configure environment
    provider = get_provider()
    provider.setup_environment()

    # Allowed tools for agent (MCP tool names without prefix for filtering)
    allowed_tools = [
        "execute_code",
        "search_pubmed",
        "search_skills",
        "update_knowledge_state",
        "save_iteration_summary",
        "set_status",
        "set_job_title",
        "read_document",
        "add_hypothesis",
        "update_hypothesis",
        "run_phenix_tool",
        "compare_structures",
        "parse_alphafold_confidence",
    ]

    # Update job status
    config["status"] = "running"
    config["started_at"] = datetime.now().isoformat()
    with open(job_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # Load skills and build system prompt
    use_skills = config.get("use_skills", True)
    skills_content = asyncio.run(
        _load_skills_for_job(job_id, config["research_question"], use_skills)
    )

    # Build system prompt with skills
    base_prompt = get_system_prompt(skills_enabled=use_skills)
    if skills_content:
        system_prompt = f"{base_prompt}\n\n{skills_content}"
        logger.info("Built system prompt with skills (%d chars)", len(system_prompt))
    else:
        system_prompt = base_prompt
        logger.info("Built system prompt without skills (%d chars)", len(system_prompt))

    # Create agent loop with MCP configuration
    agent = create_agent_loop(
        job_dir=job_dir,
        data_file=data_file,
        allowed_tools=allowed_tools,
        system_prompt=system_prompt,
    )
    logger.info("Created agent loop for job %s", job_id)

    try:
        # Prepare initial prompt
        ks = KnowledgeState.load(job_dir / "knowledge_state.json")

        # Build data context based on whether files were provided
        if config["data_files"]:
            data_context = f"""Data summary:
- Files: {config["data_files"]}
- Columns: {ks.data["data_summary"].get("columns", [])}
- Samples: {ks.data["data_summary"].get("n_samples", "Unknown")}"""
        else:
            data_context = (
                "No data files provided. You may use literature search and computational methods."
            )

        initial_prompt = f"""Begin autonomous discovery for this research question:

{config["research_question"]}

You will run for a maximum of {max_iterations} iterations.

{data_context}

You have access to MCP tools for analysis, literature search, and recording findings.
Examples include (there may be others - explore what's available):
- execute_code: Analyze data, run statistical tests, create visualizations
- search_pubmed: Search for relevant papers
- update_knowledge_state: Record confirmed findings with statistical evidence
- save_iteration_summary: Record a summary of what you investigated and learned
- set_status: Update your status to let users know what you're working on

IMPORTANT: Call set_status at the START of each significant action to update your status.
At the end of each iteration, call save_iteration_summary with a 1-2 sentence
plain-language summary of what you investigated and what you learned. This helps users
understand your investigation progress.

Start your investigation by using these tools to analyze the data.
"""

        logger.info("Starting discovery loop with agent SDK")

        # Create provenance directory for transcripts
        provenance_dir = job_dir / "provenance"
        provenance_dir.mkdir(parents=True, exist_ok=True)
        log_file = job_dir / "claude_iterations.log"

        # Iteration 1: Start session
        logger.info("Iteration 1/%d: Starting session", max_iterations)
        logger.info("Prompt length: %d characters", len(initial_prompt))
        result = asyncio.run(agent.run_iteration(initial_prompt, reset_session=True))

        if not result["success"]:
            logger.error("Iteration 1 failed: %s", result["error"])
            raise RuntimeError(f"Agent loop failed: {result['error']}")

        logger.info(
            "Iteration 1 completed successfully (tool_calls=%d)",
            result.get("tool_calls", 0),
        )

        # Get transcript and output
        transcript = result["transcript"]
        output_text = result.get("output", "")

        # Extract and save version metadata for reproducibility
        version_info = get_version_metadata()
        # Note: Without CLI, we don't have claude_code_version, but we have model info
        if version_info:
            ks = KnowledgeState.load(job_dir / "knowledge_state.json")
            ks.set_version_info(version_info)
            ks.save(job_dir / "knowledge_state.json")
            sync_knowledge_state_to_db(job_dir, ks)
            logger.info("Saved version info: %s", version_info)

        # Save transcript to provenance/
        transcript_file = provenance_dir / "iter1_transcript.json"
        with open(transcript_file, "w", encoding="utf-8") as f:
            json.dump(transcript, f, indent=2)
        logger.info("Saved transcript to %s", transcript_file)

        # Log iteration (human-readable summary)
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("=== Iteration 1 ===\n")
            f.write(f"Prompt: {initial_prompt}\n\n")
            f.write(f"Output: {output_text}\n\n")
            f.write(f"Tool calls: {result.get('tool_calls', 0)}\n\n")

        # Increment iteration counter with file locking to prevent race conditions
        # Only increment if there are more iterations to come
        ks_path = job_dir / "knowledge_state.json"
        if max_iterations > 1:
            increment_ks_iteration(ks_path)

        # Coinvestigate mode: wait for feedback after first iteration
        pending_feedback = None
        if investigation_mode == "coinvestigate" and max_iterations > 1:
            update_job_status(job_dir, "awaiting_feedback")
            pending_feedback = wait_for_feedback_or_timeout(job_dir)
            update_job_status(job_dir, "running")

        # Iterations 2-N: Resume session within batches, reset every 5 iterations
        # Note: We use --resume for short-term memory but reset periodically to:
        # 1. Prevent unbounded context growth over 10+ iterations
        # 2. Reduce cost while maintaining continuity within batches
        # 3. Agent gets reasoning context within a batch (5 iterations)
        reset_interval = 5  # Start fresh session every N iterations

        for iteration in range(2, max_iterations + 1):
            # Reload knowledge graph to see latest state
            ks = KnowledgeState.load(job_dir / "knowledge_state.json")

            # Check for feedback from previous iteration (from KS if not already captured)
            if pending_feedback is None:
                pending_feedback = ks.get_feedback_for_iteration(iteration)

            # Build feedback section if we have feedback
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
                pending_feedback = None  # Clear after using

            # Build iteration prompt
            iteration_prompt = f"""# Iteration {iteration}/{max_iterations}
{feedback_section}
{ks.get_summary()}

---

Continue your investigation using the available MCP tools.
Examples: execute_code, search_pubmed, update_knowledge_state, save_iteration_summary, set_status.
Think step by step about what will provide the most insight, then actively use the tools to execute your investigation.

Remember: Call set_status at the START of each significant action.
At the end of this iteration, call save_iteration_summary with a brief summary of what you investigated and learned."""

            # Decide whether to reset session (clears conversation history)
            should_reset = iteration % reset_interval == 1

            if should_reset:
                logger.info(
                    "Iteration %d/%d: Starting fresh session (context reset)",
                    iteration,
                    max_iterations,
                )
            else:
                logger.info(
                    "Iteration %d/%d: Continuing session",
                    iteration,
                    max_iterations,
                )

            logger.info("Prompt length: %d characters", len(iteration_prompt))
            result = asyncio.run(agent.run_iteration(iteration_prompt, reset_session=should_reset))

            if not result["success"]:
                logger.error("Iteration %d failed: %s", iteration, result["error"])
                break

            # Get transcript and output
            transcript = result["transcript"]
            output_text = result.get("output", "")

            logger.info(
                "Iteration %d completed (tool_calls=%d)",
                iteration,
                result.get("tool_calls", 0),
            )

            # Save full transcript to provenance/
            transcript_file = provenance_dir / f"iter{iteration}_transcript.json"
            with open(transcript_file, "w", encoding="utf-8") as f:
                json.dump(transcript, f, indent=2)
            logger.info("Saved transcript to %s", transcript_file)

            # Log iteration (human-readable summary)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"=== Iteration {iteration} ===\n")
                f.write(f"Prompt: {iteration_prompt}\n\n")
                f.write(f"Output: {output_text}\n\n")
                f.write(f"Tool calls: {result.get('tool_calls', 0)}\n\n")

            # Increment iteration counter with file locking to prevent race conditions
            # Only increment if this is not the last iteration
            if iteration < max_iterations:
                increment_ks_iteration(ks_path)

            # Coinvestigate mode: wait for feedback after each iteration (except last)
            if investigation_mode == "coinvestigate" and iteration < max_iterations:
                update_job_status(job_dir, "awaiting_feedback")
                pending_feedback = wait_for_feedback_or_timeout(job_dir)
                update_job_status(job_dir, "running")

        logger.info("Discovery loop completed")

        # Generate final report using SDK directly
        logger.info("Generating final report...")
        ks = KnowledgeState.load(job_dir / "knowledge_state.json")

        # Build concise summary instead of full JSON dump
        findings_summary = "\n\n".join(
            [
                f"**Finding {i + 1}: {f['title']}**\n"
                f"Evidence: {f['evidence']}\n"
                f"Interpretation: {f.get('biological_interpretation', 'N/A')}"
                for i, f in enumerate(ks.data["findings"])
            ]
        )

        literature_summary = f"Reviewed {len(ks.data['literature'])} papers from PubMed"

        report_prompt = f"""You have completed autonomous discovery. Generate a final report summarizing your findings.

Research Question: {config["research_question"]}

## Iterations Completed: {ks.data["iteration"]}

## Findings ({len(ks.data["findings"])}):
{findings_summary}

## Literature: {literature_summary}

## Analysis Log: {len(ks.data["analysis_log"])} actions performed across {ks.data["iteration"]} iterations

Create a comprehensive, accessible markdown report following these guidelines:

## Report Structure
1. **Executive Summary** (2-3 paragraphs) - Key takeaways for busy readers
2. **Key Findings** (with statistical evidence in tables)
3. **Mechanistic Model/Interpretation** - Use diagrams where helpful
4. **Knowledge Gaps Identified**
5. **Proposed Follow-up Experiments**

## Formatting Best Practices
- **Tables**: Use markdown tables to present comparative data, study results, and recommendations. Tables make numerical data scannable.
- **Citations**: Include PMID links as `[PMID: 12345678](https://pubmed.ncbi.nlm.nih.gov/12345678/)` for every referenced paper.
- **Headers**: Use proper heading hierarchy (h2 for sections, h3 for subsections) for screen reader navigation.
- **Lists**: Use bullet points for discrete items, numbered lists only for sequential steps.
- **Emphasis**: Use **bold** for key terms and conclusions, *italic* for paper titles.
- **Diagrams**: For processes or relationships, use ASCII art diagrams in code blocks.

## Accessibility Guidelines
- Write alt-text descriptions for any ASCII diagrams (as a comment before the diagram)
- Avoid conveying information through color alone
- Use descriptive link text (not "click here")
- Keep sentences concise for readability

## Psychology of Effective Reports
- Lead with the answer, then provide evidence (inverted pyramid)
- Anticipate reader questions and answer them proactively
- Quantify findings where possible (e.g., "3 of 5 studies found...")
- Acknowledge limitations and uncertainty clearly
- End with actionable next steps

Format as professional scientific markdown suitable for researchers."""

        # Generate report using SDK directly (no CLI needed)
        logger.info("Report prompt length: %d characters", len(report_prompt))

        report_generated = False
        report_error = None

        try:
            report_content = asyncio.run(
                provider.send_message(
                    messages=[{"role": "user", "content": report_prompt}],
                    max_tokens=8192,
                )
            )
            report_generated = True
        except Exception as e:  # noqa: BLE001 — catch all API errors
            report_error = str(e)
            logger.error("Report generation failed: %s", e)
            report_content = ""

        if report_generated:
            # Save Markdown report
            markdown_path = job_dir / "final_report.md"
            with open(markdown_path, "w", encoding="utf-8") as f:
                f.write(report_content)
            logger.info("Final report (Markdown) generated")
            report_generated = True

            # Generate PDF version
            try:
                from shandy.pdf_generator import markdown_to_pdf

                pdf_path = markdown_to_pdf(markdown_path, add_footer=True)
                logger.info("Final report (PDF) generated: %s", pdf_path)
            except (ValueError, OSError, ShandyError) as e:
                logger.warning("PDF generation failed (Markdown still available): %s", e)

            # Generate consensus answer (brief summary answering research question)
            consensus_prompt = f"""Based on the following research report, provide a brief consensus answer to the research question.

Research Question: {config["research_question"]}

Report:
{report_content[:8000]}

Provide a 1-3 sentence answer that directly addresses the research question based on the findings. If no clear consensus exists, state that the evidence is mixed and briefly explain why. Do not include citations or hedging language - be direct."""

            try:
                consensus_answer = asyncio.run(
                    provider.send_message(
                        messages=[{"role": "user", "content": consensus_prompt}],
                        max_tokens=1024,
                    )
                )
                consensus_answer = consensus_answer.strip()
                # Save to knowledge state
                ks_reload = KnowledgeState.load(job_dir / "knowledge_state.json")
                ks_reload.data["consensus_answer"] = consensus_answer
                ks_reload.save(job_dir / "knowledge_state.json")
                logger.info("Consensus answer generated")
            except Exception as e:  # noqa: BLE001 — catch all API errors
                logger.warning("Failed to generate consensus answer: %s", e)

        # Load final knowledge graph
        ks = KnowledgeState.load(job_dir / "knowledge_state.json")

        # Update job status - mark as failed if report generation failed
        if report_generated:
            config["status"] = "completed"
        else:
            config["status"] = "failed"
            config["error"] = f"Report generation failed: {report_error}"

        config["completed_at"] = datetime.now().isoformat()
        config["iterations_completed"] = ks.data["iteration"]
        config["findings_count"] = len(ks.data["findings"])
        # Update max_iterations to actual iterations when job stops early
        config["max_iterations"] = ks.data["iteration"]

        with open(job_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        # Sync final state to database
        sync_knowledge_state_to_db(job_dir, ks)

        return {
            "job_id": job_id,
            "status": config["status"],
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"]),
        }

    except Exception as e:  # noqa: BLE001 — top-level safety net for entire discovery run
        from shandy.version import get_version_string

        logger.error("Discovery failed [%s]: %s", get_version_string(), e, exc_info=True)

        # Update job status
        config["status"] = "failed"
        config["error"] = str(e)
        config["failed_at"] = datetime.now().isoformat()

        with open(job_dir / "config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        raise

    finally:
        # Log final token usage from agent loop
        tokens = agent.total_tokens
        logger.info(
            "Agent loop completed: %d input tokens, %d output tokens",
            tokens["input_tokens"],
            tokens["output_tokens"],
        )


def main():
    """CLI entry point for orchestrator."""
    import argparse

    parser = argparse.ArgumentParser(description="SHANDY Orchestrator")
    parser.add_argument("--job-dir", required=True, help="Job directory")
    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Run discovery
    result = run_discovery(Path(args.job_dir))

    print("\nDiscovery complete!")
    print(f"Job ID: {result['job_id']}")
    print(f"Iterations: {result['iterations']}")
    print(f"Findings: {result['findings']}")


if __name__ == "__main__":
    main()
