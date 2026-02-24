"""
Web routes for job sharing functionality.

Provides FastAPI routes for job sharing used by the NiceGUI web interface.
These routes use session-based authentication instead of API keys.
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.auth.middleware import get_current_user_id
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

# Web UI routes use session auth - exclude from API docs
router = APIRouter(prefix="/web/shares", include_in_schema=False)
CURRENT_USER_ID_DEP = Depends(get_current_user_id)
SESSION_DEP = Depends(get_session)


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


def _share_to_response(share: JobShare, target_user: User) -> ShareResponse:
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
        shared_with_email=target_user.email,
        shared_with_name=target_user.name,
        permission_level=share.permission_level,
    )


async def get_current_user_from_session(
    user_id: UUID = CURRENT_USER_ID_DEP,
    session: AsyncSession = SESSION_DEP,
) -> User:
    """
    Resolve the authenticated user for web-session routes.

    Args:
        user_id: User ID extracted from session middleware.
        session: Request-scoped database session.

    Returns:
        Active user record for downstream authorization checks.

    Raises:
        HTTPException: If the session references a missing user.
    """
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )

    return user


CURRENT_SESSION_USER_DEP = Depends(get_current_user_from_session)


@router.post("/job/{job_id}")
async def create_share(
    job_id: str,
    share_data: ShareCreate,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ShareResponse:
    """
    Create or update a share for a job owned by the current user.

    Args:
        job_id: Target job UUID string.
        share_data: Share target email and permission payload.
        user: Authenticated session user.
        session: Request-scoped database session.

    Returns:
        Persisted share metadata for the target user.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    share, target_user = await create_or_update_share(
        session,
        user.id,
        job_id=job_id,
        shared_with_email=share_data.shared_with_email,
        permission_level=share_data.permission_level,
        not_owned_detail="You can only share jobs you own",
        admin_session_factory=get_admin_session,
    )
    return _share_to_response(share, target_user)


@router.get("/job/{job_id}")
async def list_job_shares(
    job_id: str,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> list[ShareResponse]:
    """
    List all shares configured for an owned job.

    Args:
        job_id: Target job UUID string.
        user: Authenticated session user.
        session: Request-scoped database session.

    Returns:
        Shares for the job, ordered by target user email.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    shares = await list_shares_for_owned_job(
        session,
        user.id,
        job_id=job_id,
        not_owned_detail="You can only view shares for jobs you own",
    )
    return [_share_to_response(share, target_user) for share, target_user in shares]


@router.delete("/{share_id}")
async def revoke_share(
    share_id: str,
    user: User = CURRENT_SESSION_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> dict:
    """
    Revoke an existing share on a job owned by the caller.

    Args:
        share_id: Share UUID string.
        user: Authenticated session user.
        session: Request-scoped database session.

    Returns:
        Success payload consumed by the web UI.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    await revoke_share_for_owned_job(
        session,
        user.id,
        share_id=share_id,
        not_owned_detail="You can only revoke shares for jobs you own",
    )

    return {"status": "success"}


@router.get("/search/users")
async def search_users(
    q: str = Query(..., min_length=2, description="Search query"),
    limit: int = Query(10, ge=1, le=100),
    _user: User = CURRENT_SESSION_USER_DEP,
) -> list[UserSearchResult]:
    """
    Search active users for share-target autocompletion.

    Args:
        q: Case-insensitive search query.
        limit: Maximum number of users to return.
        _user: Authenticated session user (auth gate only).

    Returns:
        Matching users (id, email, display name).
    """
    users = await search_active_users(
        q,
        limit,
        admin_session_factory=get_admin_session,
    )

    return [
        UserSearchResult(
            id=str(u.id),
            email=u.email,
            name=u.name,
        )
        for u in users
    ]
