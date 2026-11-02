"""Web routes for job sharing, used by the NiceGUI interface with session auth."""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth.middleware import get_current_user_id
from openscientist.database.models import JobShare, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_admin_session, get_session
from openscientist.share_service import (
    create_or_update_share,
    list_shares_for_owned_job,
    revoke_share_for_owned_job,
    search_active_users,
)

logger = logging.getLogger(__name__)

# Session-auth routes for the web UI. Excluded from the API docs.
router = APIRouter(prefix="/web/shares", include_in_schema=False)
CURRENT_USER_ID_DEP = Depends(get_current_user_id)
SESSION_DEP = Depends(get_session)


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
    """Convert a share row and target user into a ShareResponse."""
    return ShareResponse(
        id=str(share.id),
        job_id=str(share.job_id),
        shared_with_email=target_user.email,
        shared_with_name=target_user.name,
        permission_level=share.permission_level,
    )


async def get_current_user_from_session(
    user_id: str | None = CURRENT_USER_ID_DEP,
    session: AsyncSession = SESSION_DEP,
) -> User:
    """Resolve the signed-in user for web-session routes. 401 when signed out or missing."""
    try:
        user_uuid = UUID(str(user_id)) if user_id is not None else None
    except ValueError:
        user_uuid = None
    if user_uuid is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not signed in")

    # The web login reads users under RLS, which shows a user only their own row.
    await set_current_user(session, user_uuid)
    user = (await session.execute(select(User).where(User.id == user_uuid))).scalar_one_or_none()
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
    """Create or update a share for a job owned by the current user."""
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
    """List the shares of a job owned by the current user."""
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
) -> dict[str, Any]:
    """Revoke a share on a job owned by the caller."""
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
    """Search active users for share-target autocompletion in the web UI."""
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
