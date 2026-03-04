"""Helpers for listing and assigning orphaned jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import String, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Job, User

AssignOrphanedJobReason = Literal["assigned", "job_not_found", "already_owned", "user_not_found"]


@dataclass(frozen=True)
class AssignOrphanedJobResult:
    """Result of attempting to assign an orphaned job to a user."""

    ok: bool
    reason: AssignOrphanedJobReason


async def list_orphaned_jobs(
    session: AsyncSession,
    search_query: str = "",
) -> list[Job]:
    """
    List orphaned jobs, optionally filtered by title or UUID text.

    Args:
        session: Open database session.
        search_query: Optional case-insensitive search string.

    Returns:
        Matching orphaned jobs sorted newest-first.
    """
    stmt = select(Job).where(Job.owner_id.is_(None))
    if search_query:
        stmt = stmt.where(
            Job.title.ilike(f"%{search_query}%") | Job.id.cast(String).ilike(f"%{search_query}%")
        )
    stmt = stmt.order_by(Job.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def assign_orphaned_job(
    session: AsyncSession,
    job_id: UUID,
    user_id: UUID,
) -> AssignOrphanedJobResult:
    """
    Assign an orphaned job to a user, with race-safe ownership transfer.

    The update is guarded by ``owner_id IS NULL`` so only currently orphaned jobs
    can be claimed.

    Args:
        session: Open database session.
        job_id: Job identifier.
        user_id: Target owner identifier.

    Returns:
        Structured assignment result.
    """
    user_exists = (
        await session.execute(select(User.id).where(User.id == user_id))
    ).scalar_one_or_none()
    if user_exists is None:
        return AssignOrphanedJobResult(ok=False, reason="user_not_found")

    assignment_stmt = (
        update(Job)
        .where(Job.id == job_id, Job.owner_id.is_(None))
        .values(owner_id=user_id)
        .returning(Job.id)
    )
    assigned_job_id = (await session.execute(assignment_stmt)).scalar_one_or_none()
    if assigned_job_id is not None:
        await session.commit()
        return AssignOrphanedJobResult(ok=True, reason="assigned")

    job_exists = (
        await session.execute(select(Job.id).where(Job.id == job_id))
    ).scalar_one_or_none()
    if job_exists is None:
        return AssignOrphanedJobResult(ok=False, reason="job_not_found")

    return AssignOrphanedJobResult(ok=False, reason="already_owned")
