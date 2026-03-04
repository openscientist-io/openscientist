"""
Job sharing endpoints.

Provides REST API endpoints for managing job shares, including:
- Creating and revoking shares
- Listing shares for a job
- Searching for users by email/name
"""

import logging

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.api.auth import get_current_user_from_api_key
from shandy.database.models import JobShare, User
from shandy.database.rls import set_current_user
from shandy.database.session import get_admin_session, get_session
from shandy.share_service import (
    create_or_update_share,
    list_shares_for_owned_job,
    revoke_share_for_owned_job,
    search_active_users,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shares", tags=["Shares"])
CURRENT_USER_DEP = Depends(get_current_user_from_api_key)
SESSION_DEP = Depends(get_session)


def _share_to_response(share: JobShare, target_user: User) -> "ShareResponse":
    """
    Convert persisted sharing rows into API response shape.

    Args:
        share: Job share row.
        target_user: User row referenced by ``share.shared_with_user_id``.

    Returns:
        Serialized share response.
    """
    return ShareResponse(
        id=str(share.id),
        job_id=str(share.job_id),
        shared_with_user_id=str(target_user.id),
        shared_with_email=target_user.email,
        shared_with_name=target_user.name,
        permission_level=share.permission_level,
    )


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
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ShareResponse:
    """
    Share a job with another user.

    The authenticated user must be the owner of the job.
    Users can only share jobs they own.

    Args:
        share_data: Job ID, target email, and permission payload.
        user: Authenticated API-key user.
        session: Request-scoped database session.

    Returns:
        Persisted share metadata for the target user.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    share, target_user = await create_or_update_share(
        session,
        user.id,
        job_id=share_data.job_id,
        shared_with_email=share_data.shared_with_email,
        permission_level=share_data.permission_level,
        not_owned_detail="You can only manage shares for jobs you own",
        admin_session_factory=get_admin_session,
    )
    return _share_to_response(share, target_user)


@router.get(
    "/job/{job_id}",
    response_model=ShareListResponse,
    summary="List shares for a job",
)
async def list_job_shares(
    job_id: str,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ShareListResponse:
    """
    List all shares for a specific job.

    Only the job owner can see the list of shares.

    Args:
        job_id: Target job UUID string.
        user: Authenticated API-key user.
        session: Request-scoped database session.

    Returns:
        All shares configured for the job.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    shares = await list_shares_for_owned_job(
        session,
        user.id,
        job_id=job_id,
        not_owned_detail="You can only manage shares for jobs you own",
    )

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
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> None:
    """
    Revoke a job share.

    Only the job owner can revoke shares.

    Args:
        share_id: Share UUID string.
        user: Authenticated API-key user.
        session: Request-scoped database session.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    await revoke_share_for_owned_job(
        session,
        user.id,
        share_id=share_id,
        not_owned_detail="You can only revoke shares for jobs you own",
    )


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
    _user: User = CURRENT_USER_DEP,
) -> UserSearchResponse:
    """
    Search for users by email or name.

    Returns users whose email or name contains the search query.
    Used for finding users to share jobs with.

    Args:
        q: Case-insensitive search query.
        limit: Maximum number of users to return.
        _user: Authenticated API-key user (auth gate only).

    Returns:
        Matching users plus total count.
    """
    users = await search_active_users(
        q,
        limit,
        admin_session_factory=get_admin_session,
    )

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
