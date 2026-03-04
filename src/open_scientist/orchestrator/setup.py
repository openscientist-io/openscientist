"""
Job filesystem setup for Open Scientist discovery.

create_job() initializes the job directory structure, knowledge state,
and knowledge_state.json.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from uuid import UUID

from open_scientist.async_tasks import create_background_task
from open_scientist.database.models import JobDataFile
from open_scientist.database.session import AsyncSessionLocal
from open_scientist.file_loader import get_file_info
from open_scientist.knowledge_state import KS_FILENAME, KnowledgeState
from open_scientist.orchestrator.discovery import sync_knowledge_state_to_db

logger = logging.getLogger(__name__)


def _persist_data_files_to_db(job_id: str, data_paths: list[Path]) -> None:
    """
    Persist copied job data files into ``job_data_files``.

    Uses a background task when already inside a running event loop, and falls
    back to a direct blocking write when called from synchronous contexts.

    Args:
        job_id: UUID string for the job owning the files.
        data_paths: Absolute paths to files already copied into the job folder.
    """
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
        owner_id: UUID string of the job owner. Kept for signature compatibility;
            ownership is persisted by higher-level DB job creation.

    Returns:
        Path to job directory
    """
    _ = owner_id
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    # chmod after mkdir because mode= is masked by the process umask.
    # World-writable so the agent container (non-root UID) can write into
    # the mounted job directory regardless of host/container UID mismatches.
    job_dir.chmod(0o777)

    (job_dir / "data").mkdir(exist_ok=True)
    (job_dir / "data").chmod(0o777)
    (job_dir / "provenance").mkdir(exist_ok=True)
    (job_dir / "provenance").chmod(0o777)

    data_paths: list[Path] = []
    if data_files:
        for data_file in data_files:
            original_name = Path(data_file).name
            dest = job_dir / "data" / original_name
            shutil.copy(data_file, dest)
            # chmod after copy so the agent container (non-root UID) can read it.
            dest.chmod(0o666)
            data_paths.append(dest)

    if data_paths:
        _persist_data_files_to_db(job_id, data_paths)

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

    ks_path = job_dir / KS_FILENAME
    ks.save(ks_path)
    # chmod so the agent container (non-root UID) can read and update it.
    ks_path.chmod(0o666)
    sync_knowledge_state_to_db(job_dir, ks)

    logger.info("Created job %s at %s", job_id, job_dir)
    return job_dir
