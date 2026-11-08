"""
Job filesystem setup for OpenScientist discovery.

create_job() initializes the job directory structure and database-backed
knowledge state.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from functools import partial
from pathlib import Path
from uuid import UUID

from openscientist.async_tasks import create_background_task
from openscientist.database.models import JobDataFile
from openscientist.database.session import (
    SessionOpener,
    thread_safe_admin_session,
    thread_safe_user_session,
)
from openscientist.file_loader import get_file_info
from openscientist.image_previews import PREVIEW_DIR, write_previews
from openscientist.job_container.identity import (
    apply_job_ownership,
    apply_job_ownership_tree,
)
from openscientist.knowledge_state import KnowledgeState

logger = logging.getLogger(__name__)


def _owner_session(owner_id: str | None) -> SessionOpener:
    """The owner's session, or the admin login's for a job without an owner."""
    if owner_id:
        return partial(thread_safe_user_session, UUID(owner_id))
    return thread_safe_admin_session


def _persist_data_files_to_db(
    job_id: str, data_paths: list[Path], open_session: SessionOpener
) -> None:
    """
    Persist copied job data files into ``job_data_files``.

    Uses a background task when already inside a running event loop, and falls
    back to a direct blocking write when called from synchronous contexts.

    Args:
        job_id: UUID string for the job owning the files.
        data_paths: Absolute paths to files already copied into the job folder.
        open_session: Opens the session the rows are written in.
    """
    try:

        async def _save_data_files() -> None:
            async with open_session() as session:
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

        async def _save_with_error_handling() -> None:
            try:
                await _save_data_files()
            except Exception as e:
                logger.warning("Background data file persist failed for job %s: %s", job_id, e)

        try:
            asyncio.get_running_loop()
            create_background_task(
                _save_with_error_handling(),
                name=f"persist-job-data-files-{job_id}",
                logger=logger,
            )
        except RuntimeError:
            asyncio.run(_save_data_files())
    except Exception as e:
        logger.warning("Failed to persist data files to database: %s", e)


def create_job(
    job_id: str,
    research_question: str,
    data_files: list[Path],
    max_iterations: int,
    jobs_dir: Path = Path("jobs"),
    owner_id: str | None = None,
) -> Path:
    """
    Create a new discovery job directory structure.

    Args:
        job_id: Unique job identifier
        research_question: User's research question
        data_files: List of uploaded data file paths
        max_iterations: Maximum number of iterations
        jobs_dir: Base directory for jobs
        owner_id: UUID string of the job owner. The job's rows are written in
            the owner's session, or as the admin login when there is no owner.

    Returns:
        Path to job directory
    """
    open_session = _owner_session(owner_id)
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    # Owned by the agent container's user, which the executor shares, so no
    # world bits are needed and no local account can reach the job's data.
    apply_job_ownership(job_dir)

    # Initialize a git repo so the Claude CLI gets proper gitStatus context
    # (includes "Primary working directory: <path>") for all file operations.
    subprocess.run(
        ["git", "init"],
        cwd=str(job_dir),
        capture_output=True,
        check=False,
    )
    git_dir = job_dir / ".git"
    if git_dir.exists():
        # git init leaves the repository root-owned; hand it to the agent.
        apply_job_ownership_tree(git_dir)

    (job_dir / "data").mkdir(exist_ok=True)
    apply_job_ownership(job_dir / "data")
    (job_dir / "provenance").mkdir(exist_ok=True)
    apply_job_ownership(job_dir / "provenance")

    data_paths: list[Path] = []
    if data_files:
        for data_file in data_files:
            original_name = Path(data_file).name
            dest = job_dir / "data" / original_name
            shutil.copy(data_file, dest)
            apply_job_ownership(dest)
            data_paths.append(dest)

    if data_paths:
        _persist_data_files_to_db(job_id, data_paths, open_session)
        previews = write_previews(data_paths, job_dir)
        if previews:
            apply_job_ownership(job_dir / PREVIEW_DIR)
            for preview in previews:
                apply_job_ownership(preview)

    ks = KnowledgeState(
        job_id=job_id,
        research_question=research_question,
        max_iterations=max_iterations,
    )

    if data_paths:
        first_file = data_paths[0]
        file_info = get_file_info(first_file)
        ks.set_data_summary(
            {
                "files": [str(p.name) for p in data_paths],
                "file_type": file_info["file_type"],
                "file_size_mb": file_info["size"] / (1024 * 1024),
            }
        )
    else:
        ks.set_data_summary({"files": [], "file_type": "none", "file_size_mb": 0})

    ks.save_to_database_sync(job_id, open_session=open_session)

    logger.info("Created job %s at %s", job_id, job_dir)
    return job_dir
