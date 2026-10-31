"""
The job table's rows the web process reads and writes.
"""

from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import User
from openscientist.database.models.job import Job as JobModel
from openscientist.database.models.job_share import JobShare
from openscientist.database.session import thread_safe_admin_session, thread_safe_user_session
from openscientist.job.status import status_update_statement
from openscientist.job.types import JobStatus, JobStatusUpdateResult


@asynccontextmanager
async def _job_session(user_id: UUID | None) -> AsyncIterator[AsyncSession]:
    """The user's session when a user is given, else an admin session.

    Work for no user is system work (worker threads, queue, polls), so it runs
    through the admin login and never on the web login's own rights.
    """
    if user_id is None:
        async with thread_safe_admin_session() as session:
            yield session
        return
    async with thread_safe_user_session(user_id) as session:
        yield session


async def create_job_row(
    job_id: str,
    research_question: str,
    max_iterations: int,
    use_hypotheses: bool = False,
    investigation_mode: str = "autonomous",
    owner_id: UUID | None = None,
    short_title: str | None = None,
    description: str | None = None,
    pdb_code: str | None = None,
    space_group: str | None = None,
    llm_provider: str | None = None,
    llm_config: dict[str, Any] | None = None,
) -> JobModel:
    """Create a job in the database."""
    async with _job_session(owner_id) as session:
        job = JobModel(
            id=UUID(job_id),
            owner_id=owner_id,
            research_question=research_question,
            short_title=short_title[:100] if short_title else None,
            description=description,
            use_hypotheses=use_hypotheses,
            investigation_mode=investigation_mode,
            status=JobStatus.PENDING.value,
            max_iterations=max_iterations,
            current_iteration=0,
            pdb_code=pdb_code,
            space_group=space_group,
            llm_provider=llm_provider,
            llm_config=llm_config,
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job


async def get_job(job_id: str, user_id: UUID | None = None) -> JobModel | None:
    """Get a job from the database (thread-safe for worker threads).

    With user_id, RLS limits the result to what that user may see. Without it,
    this is system work and runs as the admin login.
    """
    async with _job_session(user_id) as session:
        stmt = select(JobModel).where(JobModel.id == UUID(job_id))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_share_permission(job_id: str, user_id: UUID) -> str | None:
    """Return the share permission level for a user on a job.

    Returns 'view', 'edit', or None if the user has no share.
    """
    async with _job_session(user_id) as session:
        stmt = select(JobShare.permission_level).where(
            JobShare.job_id == UUID(job_id),
            JobShare.shared_with_user_id == user_id,
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def set_job_status(
    job_id: str,
    status: JobStatus,
    *,
    allowed_from: Collection[JobStatus],
    error_message: str | None = None,
    user_id: UUID | None = None,
    cancellation_reason: str | None = None,
) -> JobStatusUpdateResult:
    """Move the job to ``status`` if it is in one of ``allowed_from`` (thread-safe)."""
    result = JobStatusUpdateResult()
    # Only the worker moves a job to running, so this is when it takes a slot.
    stmt = status_update_statement(
        UUID(job_id),
        status,
        allowed_from=allowed_from,
        error_message=error_message,
        cancellation_reason=cancellation_reason,
        takes_slot=status == JobStatus.RUNNING,
    ).returning(JobModel)

    async with _job_session(user_id) as session:
        job = (await session.execute(stmt)).scalar_one_or_none()
        if job is None:
            return result
        result.applied = True
        await session.commit()
        await _fill_notification_target(session, job, result)

    return result


async def _fill_notification_target(
    session: AsyncSession, job: JobModel, result: JobStatusUpdateResult
) -> None:
    """Copy the job fields a notification shows and the owner's ntfy settings."""
    result.owner_id = str(job.owner_id) if job.owner_id else None
    result.job_title = job.short_title or job.research_question
    result.current_iteration = job.current_iteration
    if job.owner_id:
        user_stmt = select(User.ntfy_enabled, User.ntfy_topic).where(User.id == job.owner_id)
        user_row = (await session.execute(user_stmt)).first()
        if user_row:
            result.ntfy_enabled = user_row.ntfy_enabled
            result.ntfy_topic = user_row.ntfy_topic


async def get_notification_target(job_id: str) -> JobStatusUpdateResult:
    """Notification data for a status the agent container wrote."""
    result = JobStatusUpdateResult()
    async with _job_session(None) as session:
        stmt = select(JobModel).where(JobModel.id == UUID(job_id))
        job = (await session.execute(stmt)).scalar_one_or_none()
        if job:
            await _fill_notification_target(session, job, result)
    return result


async def next_queued_job(window: timedelta, exclude: Collection[str]) -> str | None:
    """The queued job to start next: a priority owner's job goes ahead of every
    other, then the owner whose jobs held a slot for the fewest seconds in
    ``window``, running ones up to now, and among equals the oldest job goes
    first. Jobs in ``exclude`` already have a thread (thread-safe)."""
    stmt = text(
        """
        WITH since AS (SELECT now() - CAST(:window AS interval) AS t),
        usage AS (
            SELECT owner_id,
                   sum(extract(epoch FROM coalesce(ended_at, now()) - greatest(started_at, t)))
                       AS seconds
            FROM jobs, since
            WHERE started_at IS NOT NULL AND coalesce(ended_at, now()) > t
            GROUP BY owner_id
        )
        SELECT j.id FROM jobs j
        LEFT JOIN users o ON o.id = j.owner_id
        LEFT JOIN usage u ON u.owner_id = j.owner_id
        WHERE j.status = 'queued' AND NOT (j.id::text = ANY(CAST(:exclude AS text[])))
        ORDER BY coalesce(o.is_priority, false) DESC, coalesce(u.seconds, 0), j.created_at
        LIMIT 1
        """
    )
    async with _job_session(None) as session:
        job_id = await session.scalar(stmt, {"window": window, "exclude": list(exclude)})
    return None if job_id is None else str(job_id)


async def list_jobs(
    status: JobStatus | None = None,
    limit: int | None = None,
    user_id: UUID | None = None,
) -> list[JobModel]:
    """List jobs from the database (thread-safe for worker threads)."""
    async with _job_session(user_id) as session:
        stmt = select(JobModel).order_by(JobModel.created_at.desc())

        if status:
            stmt = stmt.where(JobModel.status == status.value)

        if limit:
            stmt = stmt.limit(limit)

        result = await session.execute(stmt)
        return list(result.scalars().all())


async def delete_job(job_id: str, user_id: UUID | None = None) -> None:
    """Delete a job from the database (thread-safe for worker threads)."""
    async with _job_session(user_id) as session:
        stmt = select(JobModel).where(JobModel.id == UUID(job_id))
        result = await session.execute(stmt)
        job = result.scalar_one_or_none()

        if job:
            await session.delete(job)
            await session.commit()
