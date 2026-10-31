"""Shared business logic for job sharing.

Both API-key and web-session routes delegate here so share authorization stays consistent.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import or_, select
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
) -> list[tuple[JobShare, User]]:
    """List the shares of a job after verifying ownership."""
    job = await get_owned_job(
        session,
        owner_id,
        job_id,
        not_owned_detail=not_owned_detail,
    )

    result = await session.execute(
        select(JobShare, User)
        .join(User, JobShare.shared_with_user_id == User.id)
        .where(JobShare.job_id == job.id)
        .order_by(User.email)
    )
    return list(result.tuples().all())


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


async def search_active_users(
    query: str,
    limit: int,
    *,
    admin_session_factory: AdminSessionFactory = get_admin_session,
) -> list[User]:
    """Search active users by email or display name through an admin-scoped session."""
    search_pattern = f"%{query}%"
    async with admin_session_factory() as admin_session:
        result = await admin_session.execute(
            select(User)
            .where(
                or_(
                    User.email.ilike(search_pattern),
                    User.name.ilike(search_pattern),
                )
            )
            .where(User.is_active.is_(True))
            .order_by(User.email)
            .limit(limit)
        )
        return list(result.scalars().all())
