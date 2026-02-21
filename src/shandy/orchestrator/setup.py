"""
Job filesystem setup for SHANDY discovery.

create_job() initializes the job directory structure, knowledge state,
and config.json.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

from shandy.file_loader import get_file_info
from shandy.knowledge_state import KnowledgeState
from shandy.orchestrator.discovery import _persist_data_files_to_db, sync_knowledge_state_to_db

logger = logging.getLogger(__name__)


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
    Create a new discovery job directory structure.

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

    (job_dir / "data").mkdir(exist_ok=True)
    (job_dir / "provenance").mkdir(exist_ok=True)

    data_paths: list[Path] = []
    if data_files:
        for data_file in data_files:
            original_name = Path(data_file).name
            dest = job_dir / "data" / original_name
            shutil.copy(data_file, dest)
            data_paths.append(dest)

    if data_paths:
        owner_uuid = UUID(owner_id) if owner_id else None
        _persist_data_files_to_db(job_id, job_dir, data_paths, owner_uuid)

    ks = KnowledgeState(
        job_id=job_id,
        research_question=research_question,
        max_iterations=max_iterations,
        use_skills=use_skills,
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

    ks.save(job_dir / "knowledge_state.json")
    sync_knowledge_state_to_db(job_dir, ks)

    config = {
        "job_id": job_id,
        "research_question": research_question,
        "data_files": [str(p) for p in data_paths],
        "max_iterations": max_iterations,
        "use_skills": use_skills,
        "investigation_mode": investigation_mode,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "created",
        "owner_id": owner_id,
        "ntfy_enabled": ntfy_enabled,
        "ntfy_topic": ntfy_topic,
    }

    with open(job_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    logger.info("Created job %s at %s", job_id, job_dir)
    return job_dir
