"""Standalone job-metadata tools (`set_status`, `set_job_title`,
`save_iteration_summary`, `set_consensus_answer`)."""

from __future__ import annotations

import logging
from uuid import UUID

from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import async_session_local
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE

logger = logging.getLogger(__name__)

_MAX_TITLE_LENGTH = 100
_MIN_TITLE_LENGTH = 3


def _validate_title(title: str) -> str | None:
    if len(title) > _MAX_TITLE_LENGTH:
        return f"❌ Title too long ({len(title)} chars). Please keep it under 100 characters."
    if len(title) < _MIN_TITLE_LENGTH:
        return "❌ Title too short. Please provide a meaningful title."
    return None


@mcp.tool()
async def set_status(message: str) -> str:
    """Update the agent's current status message (shown in the UI).

    Args:
        message: Status message (max 80 characters, e.g., 'Running PCA on expression data')

    Returns:
        Confirmation
    """
    trimmed = message[:80]
    ks = await KnowledgeState.load_from_database(STATE.job_id)
    ks.set_agent_status(trimmed)
    await ks.save_to_database(STATE.job_id)
    return f"✅ Status updated: {trimmed}"


@mcp.tool()
async def set_job_title(title: str) -> str:
    """Set a brief, descriptive title for this job.

    Args:
        title: Short title (3-100 characters)

    Returns:
        Confirmation
    """
    validation_error = _validate_title(title)
    if validation_error:
        return validation_error
    try:
        job_uuid = UUID(STATE.job_id)
    except ValueError:
        return f"❌ Invalid job id: {STATE.job_id}"
    try:
        async with async_session_local() as session:
            job = await session.get(JobModel, job_uuid)
            if job is None:
                return "❌ Job not found in database."
            job.short_title = title
            await session.commit()
    except Exception as e:
        logger.warning("Failed to persist job title to database: %s", e)
        return "❌ Failed to persist job title."
    return f"✅ Job title set: {title}"


@mcp.tool()
async def save_iteration_summary(summary: str, strapline: str = "") -> str:
    """Save a summary of this iteration's investigation and findings.

    Call this at the end of each iteration.

    Args:
        summary: 1-2 sentence summary of what you investigated and learned
        strapline: Optional one-line headline for this iteration

    Returns:
        Confirmation
    """
    ks = await KnowledgeState.load_from_database(STATE.job_id)
    ks.add_iteration_summary(
        iteration=ks.data["iteration"],
        summary=summary,
        strapline=strapline,
    )
    await ks.save_to_database(STATE.job_id)
    return f"✅ Iteration summary saved: {summary[:100]}"


@mcp.tool()
async def set_consensus_answer(answer: str) -> str:
    """Set the consensus answer to the research question (1-3 sentences, direct).

    Call this after writing the final report.

    Args:
        answer: A direct 1-3 sentence answer to the research question

    Returns:
        Confirmation
    """
    ks = await KnowledgeState.load_from_database(STATE.job_id)
    ks.data["consensus_answer"] = answer.strip()
    await ks.save_to_database(STATE.job_id)
    return "✅ Consensus answer set"
