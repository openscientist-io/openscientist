"""
Job metadata tools for the SDK agent path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from shandy.tools.registry import ToolContext, tool

logger = logging.getLogger(__name__)

_MAX_TITLE_LENGTH = 100
_MIN_TITLE_LENGTH = 3


def _status_path(ctx: ToolContext) -> Path:
    return ctx.job_dir / "knowledge_state.json"


def _set_status_impl(ctx: ToolContext, message: str) -> str:
    from shandy.knowledge_state import KnowledgeState

    ks_path = _status_path(ctx)
    trimmed = message[:80]
    ks = KnowledgeState.load(ks_path)
    ks.set_agent_status(trimmed)
    ks.save(ks_path)
    return f"✅ Status updated: {trimmed}"


def _validate_job_title(title: str) -> str | None:
    if len(title) > _MAX_TITLE_LENGTH:
        return f"❌ Title too long ({len(title)} chars). Please keep it under 100 characters."
    if len(title) < _MIN_TITLE_LENGTH:
        return "❌ Title too short. Please provide a meaningful title."
    return None


def _job_uuid_or_error(ctx: ToolContext) -> tuple[UUID | None, str | None]:
    job_id = ctx.job_dir.name
    try:
        return UUID(job_id), None
    except ValueError:
        return None, f"❌ Invalid job id: {job_id}"


async def _update_job_title_in_db(job_uuid: UUID, title: str) -> bool:
    from shandy.database.models.job import Job as JobModel
    from shandy.database.session import AsyncSessionLocal

    async with AsyncSessionLocal(thread_safe=True) as session:
        job = await session.get(JobModel, job_uuid)
        if job is None:
            return False
        job.short_title = title
        await session.commit()
        return True


def _persist_job_title(ctx: ToolContext, title: str) -> str | None:
    import asyncio

    from shandy.async_tasks import create_background_task

    job_uuid, error = _job_uuid_or_error(ctx)
    if error:
        return error
    if job_uuid is None:
        return "❌ Invalid job id."

    update_coro = _update_job_title_in_db(job_uuid, title)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if not asyncio.run(update_coro):
            return "❌ Job not found in database."
    else:
        create_background_task(
            update_coro,
            name=f"set-job-title-{job_uuid}",
            logger=logger,
        )
    return None


def _set_job_title_impl(ctx: ToolContext, title: str) -> str:
    validation_error = _validate_job_title(title)
    if validation_error:
        return validation_error

    try:
        persist_error = _persist_job_title(ctx, title)
    except Exception as e:
        logger.warning("Failed to persist job title to database: %s", e)
        return "❌ Failed to persist job title."

    if persist_error:
        return persist_error
    return f"✅ Job title set: {title}"


def _save_iteration_summary_impl(ctx: ToolContext, summary: str, strapline: str = "") -> str:
    from shandy.knowledge_state import KnowledgeState

    ks_path = _status_path(ctx)
    ks = KnowledgeState.load(ks_path)
    ks.add_iteration_summary(
        iteration=ks.data["iteration"],
        summary=summary,
        strapline=strapline,
    )
    ks.save(ks_path)
    return f"✅ Iteration summary saved: {summary[:100]}"


def _set_consensus_answer_impl(ctx: ToolContext, answer: str) -> str:
    from shandy.knowledge_state import KnowledgeState

    ks_path = _status_path(ctx)
    ks = KnowledgeState.load(ks_path)
    ks.data["consensus_answer"] = answer.strip()
    ks.save(ks_path)
    return "✅ Consensus answer set"


def make_tools(ctx: ToolContext) -> list[Callable[..., Any]]:
    """Return job metadata tools (set_status, set_job_title, save_iteration_summary)."""

    @tool
    def set_status(message: str) -> str:
        """
        Update the agent's current status message (shown in the UI).

        Args:
            message: Status message (max 80 characters, e.g., 'Running PCA on expression data')

        Returns:
            Confirmation
        """
        return _set_status_impl(ctx, message)

    @tool
    def set_job_title(title: str) -> str:
        """
        Set a brief, descriptive title for this job.

        Args:
            title: Short title (3-100 characters)

        Returns:
            Confirmation
        """
        return _set_job_title_impl(ctx, title)

    @tool
    def save_iteration_summary(summary: str, strapline: str = "") -> str:
        """
        Save a summary of this iteration's investigation and findings.

        Call this at the end of each iteration.

        Args:
            summary: 1-2 sentence summary of what you investigated and learned
            strapline: Optional one-line headline for this iteration

        Returns:
            Confirmation
        """
        return _save_iteration_summary_impl(ctx, summary, strapline)

    @tool
    def set_consensus_answer(answer: str) -> str:
        """
        Set the consensus answer to the research question (1-3 sentences, direct).

        Call this after writing the final report.

        Args:
            answer: A direct 1-3 sentence answer to the research question

        Returns:
            Confirmation
        """
        return _set_consensus_answer_impl(ctx, answer)

    return [set_status, set_job_title, save_iteration_summary, set_consensus_answer]
