"""Shared business logic for job sharing.

Both API-key and web-session routes delegate here so share authorization stays consistent.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.api.utils import parse_uuid
from openscientist.database.models import Job, JobShare, User
from openscientist.database.session import get_admin_session

AdminSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def get_owned_job(
    session: AsyncSession,
    owner_id: UUID,
    job_id: str,
    *,
    not_owned_detail: str,
) -> Job:
    """Load a job, raising 404 when missing and 403 when not owned by the caller."""
    job_uuid = parse_uuid(job_id, "job_id")
    result = await session.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or you don't have access",
        )

    if job.owner_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=not_owned_detail,
        )

    return job


async def find_user_by_email(
    email: str,
    *,
    admin_session_factory: AdminSessionFactory = get_admin_session,
) -> User:
    """Resolve an active user by email through an admin-scoped session, bypassing RLS."""
    async with admin_session_factory() as admin_session:
        result = await admin_session.execute(
            select(User).where(
                User.email == email,
                User.is_active.is_(True),
            )
        )
        target_user = result.scalar_one_or_none()

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with email '{email}' not found",
        )

    return target_user


async def create_or_update_share(
    session: AsyncSession,
    owner_id: UUID,
    *,
    job_id: str,
    shared_with_email: str,
    permission_level: str,
    not_owned_detail: str,
    admin_session_factory: AdminSessionFactory = get_admin_session,
) -> tuple[JobShare, User]:
    """Create a job share, or update the permission if the share already exists."""
    job = await get_owned_job(
        session,
        owner_id,
        job_id,
        not_owned_detail=not_owned_detail,
    )
    target_user = await find_user_by_email(
        shared_with_email,
        admin_session_factory=admin_session_factory,
    )

    if target_user.id == owner_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot share job with yourself",
        )

    existing_result = await session.execute(
        select(JobShare).where(
            JobShare.job_id == job.id,
            JobShare.shared_with_user_id == target_user.id,
        )
    )
    existing_share = existing_result.scalar_one_or_none()

    if existing_share:
        existing_share.permission_level = permission_level
        await session.commit()
        await session.refresh(existing_share)
        return existing_share, target_user

    new_share = JobShare(
        job_id=job.id,
        shared_with_user_id=target_user.id,
        permission_level=permission_level,
    )
    session.add(new_share)
    await session.commit()
    await session.refresh(new_share)
    return new_share, target_user


async def list_shares_for_owned_job(
    session: AsyncSession,
    owner_id: UUID,
    *,
    job_id: str,
    not_owned_detail: str,
    admin_session_factory: AdminSessionFactory = get_admin_session,
) -> list[tuple[JobShare, User]]:
    """List the shares of a job after verifying ownership.

    The shared-with users are resolved through an admin-scoped session,
    because the owner's RLS session shows only their own user row.
    """
    job = await get_owned_job(
        session,
        owner_id,
        job_id,
        not_owned_detail=not_owned_detail,
    )

    result = await session.execute(select(JobShare).where(JobShare.job_id == job.id))
    shares = list(result.scalars().all())

    if not shares:
        return []

    async with admin_session_factory() as admin_session:
        targets = (
            (
                await admin_session.execute(
                    select(User).where(User.id.in_([share.shared_with_user_id for share in shares]))
                )
            )
            .scalars()
            .all()
        )

    by_id = {target.id: target for target in targets}
    resolved = [
        (share, by_id[share.shared_with_user_id])
        for share in shares
        if share.shared_with_user_id in by_id
    ]
    resolved.sort(key=lambda pair: pair[1].email)
    return resolved


async def revoke_share_for_owned_job(
    session: AsyncSession,
    owner_id: UUID,
    *,
    share_id: str,
    not_owned_detail: str,
) -> None:
    """Delete a share, after verifying the caller owns its job."""
    share_uuid = parse_uuid(share_id, "share_id")
    share_result = await session.execute(select(JobShare).where(JobShare.id == share_uuid))
    share = share_result.scalar_one_or_none()

    if not share:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share not found",
        )

    job_result = await session.execute(select(Job).where(Job.id == share.job_id))
    job = job_result.scalar_one_or_none()
    if not job or job.owner_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=not_owned_detail,
        )

    await session.delete(share)
    await session.commit()


async def find_share_target_by_email(
    email: str,
    *,
    admin_session_factory: AdminSessionFactory = get_admin_session,
) -> User | None:
    """Resolve at most one active user by exact, case-insensitive email.

    Returns None when the email is empty or no active user uses it. The caller
    enforces the approval gate, which needs the caller's RLS session.
    """
    needle = email.strip().lower()
    if not needle:
        return None
    async with admin_session_factory() as admin_session:
        result = await admin_session.execute(
            select(User).where(
                func.lower(User.email) == needle,
                User.is_active.is_(True),
            )
        )
        # Two accounts may differ only by case, which the unique email index allows.
        return result.scalars().first()


# Top-level job-directory entries withheld from a viewer who can see a job only
# because it is public. ``data/`` holds the owner's uploaded input files.
PUBLIC_VIEWER_EXCLUDED_DIRS: tuple[str, ...] = ("data",)


async def set_job_public(
    session: AsyncSession,
    owner_id: UUID,
    *,
    job_id: str,
    is_public: bool,
    not_owned_detail: str,
) -> Job:
    """Make an owned job readable by any signed-in user, or private again.

    Raises 403 for anyone who does not own the job, including users who cannot
    see it at all.
    """
    job_uuid = parse_uuid(job_id, "job_id")
    result = await session.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()

    if job is None or job.owner_id != owner_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=not_owned_detail,
        )

    job.is_public = is_public
    await session.commit()
    await session.refresh(job)
    return job


async def viewer_has_direct_access(session: AsyncSession, job: Job, user_id: UUID) -> bool:
    """Whether the user owns the job or has it shared, as opposed to seeing it
    only because it is public."""
    if job.owner_id == user_id:
        return True
    result = await session.execute(
        select(JobShare.id).where(
            JobShare.job_id == job.id,
            JobShare.shared_with_user_id == user_id,
        )
    )
    return result.first() is not None


async def withheld_artifact_dirs(session: AsyncSession, job: Job, user_id: UUID) -> tuple[str, ...]:
    """Job-directory entries to leave out for a viewer who reads the job only
    because it is public; the owner and share recipients get everything."""
    if await viewer_has_direct_access(session, job, user_id):
        return ()
    return PUBLIC_VIEWER_EXCLUDED_DIRS
