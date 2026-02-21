"""
Web routes for job sharing functionality.

Provides FastAPI routes for job sharing used by the NiceGUI web interface.
These routes use session-based authentication instead of API keys.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.auth.middleware import get_current_user_id
from shandy.database.models import Job, JobShare, User
from shandy.database.rls import set_current_user
from shandy.database.session import get_session

logger = logging.getLogger(__name__)

# Web UI routes use session auth - exclude from API docs
router = APIRouter(prefix="/web/shares", include_in_schema=False)


# Pydantic models for request/response
class ShareCreate(BaseModel):
    """Request body for creating a job share."""

    shared_with_email: str = Field(
        ...,
        description="Email address of user to share with",
    )
    permission_level: str = Field(
        "view",
        description="Permission level: 'view' or 'edit'",
        pattern="^(view|edit)$",
    )


class ShareResponse(BaseModel):
    """Response for a job share."""

    id: str
    job_id: str
    shared_with_email: str
    shared_with_name: str
    permission_level: str


class UserSearchResult(BaseModel):
    """Result for user search."""

    id: str
    email: str
    name: str


async def get_current_user_from_session(
    user_id: UUID = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Get the current user from session authentication."""
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return user


@router.post("/job/{job_id}")
async def create_share(
    job_id: str,
    share_data: ShareCreate,
    user: User = Depends(get_current_user_from_session),
    session: AsyncSession = Depends(get_session),
) -> ShareResponse:
    """Share a job with another user."""
    # Set RLS context
    await set_current_user(session, user.id)

    # Verify job exists and user owns it
    job_stmt = select(Job).where(Job.id == UUID(job_id))
    job_result = await session.execute(job_stmt)
    job = job_result.scalar_one_or_none()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or you don't have access",
        )

    if job.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only share jobs you own",
        )

    # Find user to share with by email
    target_stmt = select(User).where(User.email == share_data.shared_with_email)
    target_result = await session.execute(target_stmt)
    target_user = target_result.scalar_one_or_none()

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with email '{share_data.shared_with_email}' not found",
        )

    # Prevent sharing with self
    if target_user.id == user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot share job with yourself",
        )

    # Check if share already exists
    share_check_stmt = select(JobShare).where(
        JobShare.job_id == UUID(job_id),
        JobShare.shared_with_user_id == target_user.id,
    )
    share_check_result = await session.execute(share_check_stmt)
    existing_share = share_check_result.scalar_one_or_none()

    if existing_share:
        # Update existing share permission
        existing_share.permission_level = share_data.permission_level
        await session.commit()
        await session.refresh(existing_share)

        return ShareResponse(
            id=str(existing_share.id),
            job_id=str(existing_share.job_id),
            shared_with_email=target_user.email,
            shared_with_name=target_user.name,
            permission_level=existing_share.permission_level,
        )

    # Create new share
    new_share = JobShare(
        job_id=UUID(job_id),
        shared_with_user_id=target_user.id,
        permission_level=share_data.permission_level,
    )
    session.add(new_share)
    await session.commit()
    await session.refresh(new_share)

    return ShareResponse(
        id=str(new_share.id),
        job_id=str(new_share.job_id),
        shared_with_email=target_user.email,
        shared_with_name=target_user.name,
        permission_level=new_share.permission_level,
    )


@router.get("/job/{job_id}")
async def list_job_shares(
    job_id: str,
    user: User = Depends(get_current_user_from_session),
    session: AsyncSession = Depends(get_session),
) -> list[ShareResponse]:
    """List all shares for a specific job."""
    # Set RLS context
    await set_current_user(session, user.id)

    # Verify job exists and user owns it
    job_stmt = select(Job).where(Job.id == UUID(job_id))
    job_result = await session.execute(job_stmt)
    job = job_result.scalar_one_or_none()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or you don't have access",
        )

    if job.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only view shares for jobs you own",
        )

    # Get all shares for this job with user info
    shares_stmt = (
        select(JobShare, User)
        .join(User, JobShare.shared_with_user_id == User.id)
        .where(JobShare.job_id == UUID(job_id))
        .order_by(User.email)
    )
    shares_result = await session.execute(shares_stmt)
    shares = shares_result.all()

    return [
        ShareResponse(
            id=str(share.id),
            job_id=str(share.job_id),
            shared_with_email=target_user.email,
            shared_with_name=target_user.name,
            permission_level=share.permission_level,
        )
        for share, target_user in shares
    ]


@router.delete("/{share_id}")
async def revoke_share(
    share_id: str,
    user: User = Depends(get_current_user_from_session),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Revoke a job share."""
    # Set RLS context
    await set_current_user(session, user.id)

    # Find the share
    share_stmt = select(JobShare).where(JobShare.id == UUID(share_id))
    share_result = await session.execute(share_stmt)
    share = share_result.scalar_one_or_none()

    if not share:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share not found",
        )

    # Verify user owns the job
    job_stmt = select(Job).where(Job.id == share.job_id)
    job_result = await session.execute(job_stmt)
    job = job_result.scalar_one_or_none()

    if not job or job.owner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only revoke shares for jobs you own",
        )

    # Delete the share
    await session.delete(share)
    await session.commit()

    return {"status": "success"}


@router.get("/search/users")
async def search_users(
    q: str = Query(..., min_length=2, description="Search query"),
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(get_current_user_from_session),
    session: AsyncSession = Depends(get_session),
) -> list[UserSearchResult]:
    """Search for users by email or name."""
    # Search users by email or name (case-insensitive)
    search_pattern = f"%{q}%"
    stmt = (
        select(User)
        .where(
            or_(
                User.email.ilike(search_pattern),
                User.name.ilike(search_pattern),
            )
        )
        .where(User.is_active == True)  # noqa: E712
        .order_by(User.email)
        .limit(limit)
    )
    result = await session.execute(stmt)
    users = result.scalars().all()

    return [
        UserSearchResult(
            id=str(u.id),
            email=u.email,
            name=u.name,
        )
        for u in users
    ]
