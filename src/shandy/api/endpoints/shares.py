"""
Job sharing endpoints.

Provides REST API endpoints for managing job shares, including:
- Creating and revoking shares
- Listing shares for a job
- Searching for users by email/name
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.api.auth import get_current_user_from_api_key
from shandy.database.models import Job, JobShare, User
from shandy.database.rls import set_current_user
from shandy.database.session import get_admin_session, get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shares", tags=["Shares"])


def _parse_uuid(value: str, field_name: str) -> UUID:
    """Parse UUID input and raise a client error on invalid format."""
    try:
        return UUID(value)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} format",
        ) from e


def _share_to_response(share: "JobShare", target_user: "User") -> "ShareResponse":
    """Convert a JobShare + User pair to a ShareResponse."""
    return ShareResponse(
        id=str(share.id),
        job_id=str(share.job_id),
        shared_with_user_id=str(target_user.id),
        shared_with_email=target_user.email,
        shared_with_name=target_user.name,
        permission_level=share.permission_level,
    )


async def _get_owned_job(session: "AsyncSession", user: "User", job_id: str) -> "Job":
    """Get a job by ID, raising if not found or not owned by user."""
    job_uuid = _parse_uuid(job_id, "job_id")
    job_stmt = select(Job).where(Job.id == job_uuid)
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
            detail="You can only manage shares for jobs you own",
        )

    return job


# Pydantic models for request/response
class ShareCreate(BaseModel):
    """Request body for creating a job share."""

    job_id: str = Field(
        ...,
        description="Job ID to share",
        examples=["01933e34-8f6e-7890-abcd-ef1234567890"],
    )
    shared_with_email: str = Field(
        ...,
        description="Email address of user to share with",
        examples=["colleague@example.com"],
    )
    permission_level: str = Field(
        "view",
        description="Permission level: 'view' or 'edit'",
        pattern="^(view|edit)$",
    )


class ShareResponse(BaseModel):
    """Response for a job share."""

    id: str = Field(..., description="Share ID")
    job_id: str = Field(..., description="Job ID")
    shared_with_user_id: str = Field(..., description="User ID")
    shared_with_email: str = Field(..., description="Email address")
    shared_with_name: str = Field(..., description="Display name")
    permission_level: str = Field(..., description="Permission level (view/edit)")


class ShareListResponse(BaseModel):
    """Response for listing shares."""

    shares: list[ShareResponse] = Field(..., description="List of shares")
    total: int = Field(..., description="Total number of shares")


class UserSearchResult(BaseModel):
    """Result for user search."""

    id: str = Field(..., description="User ID")
    email: str = Field(..., description="Email address")
    name: str = Field(..., description="Display name")


class UserSearchResponse(BaseModel):
    """Response for user search."""

    users: list[UserSearchResult] = Field(..., description="Matching users")
    total: int = Field(..., description="Total number of results")


@router.post(
    "",
    response_model=ShareResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Share a job with another user",
)
async def create_share(
    share_data: ShareCreate,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> ShareResponse:
    """
    Share a job with another user.

    The authenticated user must be the owner of the job.
    Users can only share jobs they own.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    # Verify job exists and user owns it
    await _get_owned_job(session, user, share_data.job_id)

    # Find user to share with by email (use admin session to search all users)
    async with get_admin_session() as admin_session:
        target_stmt = select(User).where(User.email == share_data.shared_with_email)
        target_result = await admin_session.execute(target_stmt)
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
    job_uuid = _parse_uuid(share_data.job_id, "job_id")

    share_stmt = select(JobShare).where(
        JobShare.job_id == job_uuid,
        JobShare.shared_with_user_id == target_user.id,
    )
    share_result = await session.execute(share_stmt)
    existing_share = share_result.scalar_one_or_none()

    if existing_share:
        # Update existing share permission
        existing_share.permission_level = share_data.permission_level
        await session.commit()
        await session.refresh(existing_share)

        return _share_to_response(existing_share, target_user)

    # Create new share
    new_share = JobShare(
        job_id=job_uuid,
        shared_with_user_id=target_user.id,
        permission_level=share_data.permission_level,
    )
    session.add(new_share)
    await session.commit()
    await session.refresh(new_share)

    return _share_to_response(new_share, target_user)


@router.get(
    "/job/{job_id}",
    response_model=ShareListResponse,
    summary="List shares for a job",
)
async def list_job_shares(
    job_id: str,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> ShareListResponse:
    """
    List all shares for a specific job.

    Only the job owner can see the list of shares.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    # Verify job exists and user owns it
    await _get_owned_job(session, user, job_id)
    job_uuid = _parse_uuid(job_id, "job_id")

    # Get all shares for this job with user info
    shares_stmt = (
        select(JobShare, User)
        .join(User, JobShare.shared_with_user_id == User.id)
        .where(JobShare.job_id == job_uuid)
        .order_by(User.email)
    )
    shares_result = await session.execute(shares_stmt)
    shares = shares_result.all()

    return ShareListResponse(
        shares=[_share_to_response(share, target_user) for share, target_user in shares],
        total=len(shares),
    )


@router.delete(
    "/{share_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a job share",
)
async def revoke_share(
    share_id: str,
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> None:
    """
    Revoke a job share.

    Only the job owner can revoke shares.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    # Find the share
    share_uuid = _parse_uuid(share_id, "share_id")
    share_stmt = select(JobShare).where(JobShare.id == share_uuid)
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


@router.get(
    "/search/users",
    response_model=UserSearchResponse,
    summary="Search for users by email or name",
)
async def search_users(
    q: str = Query(
        ...,
        min_length=2,
        description="Search query (email or name)",
        examples=["alice@example.com"],
    ),
    limit: int = Query(
        10,
        ge=1,
        le=100,
        description="Maximum number of results",
    ),
    user: User = Depends(get_current_user_from_api_key),
    session: AsyncSession = Depends(get_session),
) -> UserSearchResponse:
    """
    Search for users by email or name.

    Returns users whose email or name contains the search query.
    Used for finding users to share jobs with.
    """
    # Search users by email or name (use admin session to search all users)
    search_pattern = f"%{q}%"
    async with get_admin_session() as admin_session:
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
        result = await admin_session.execute(stmt)
        users = result.scalars().all()

    return UserSearchResponse(
        users=[
            UserSearchResult(
                id=str(u.id),
                email=u.email,
                name=u.name,
            )
            for u in users
        ],
        total=len(users),
    )
