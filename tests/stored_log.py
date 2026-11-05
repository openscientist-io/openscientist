"""The analysis log entries a job has stored, for tests of the tools that write them."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from openscientist.database import async_session_local
from openscientist.database.models.analysis_log import AnalysisLog
from openscientist.knowledge_state_db import load_iteration_analysis_log


async def last_logged_entry(job_id: str | UUID) -> dict[str, Any]:
    """The job's most recently stored analysis log entry, in knowledge state format."""
    job_uuid = UUID(str(job_id))
    async with async_session_local() as session:
        iteration = await session.scalar(
            select(func.max(AnalysisLog.iteration)).where(AnalysisLog.job_id == job_uuid)
        )
        assert iteration is not None, "the job has stored no analysis log entry"
        entries = await load_iteration_analysis_log(
            session, job_uuid, iteration, output_chars=1_000_000
        )
    return entries[-1]
