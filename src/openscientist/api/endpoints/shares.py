"""Job sharing endpoints."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.api.auth import get_current_user_from_api_key
from openscientist.database.models import JobShare, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_admin_session, get_session
from openscientist.share_service import (
    create_or_update_share,
    find_share_target_by_email,
    list_shares_for_owned_job,
    revoke_share_for_owned_job,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shares", tags=["Shares"])
CURRENT_USER_DEP = Depends(get_current_user_from_api_key)
SESSION_DEP = Depends(get_session)


def _share_to_response(share: JobShare, target_user: User) -> "ShareResponse":
    """Convert a share row and target user into a ShareResponse."""
    return ShareResponse(
        id=str(share.id),
        job_id=str(share.job_id),
        shared_with_user_id=str(target_user.id),
        shared_with_email=target_user.email,
        shared_with_name=target_user.name,
        permission_level=share.permission_level,
    )


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


class ShareTarget(BaseModel):
    """A resolved share target."""

    id: str = Field(..., description="User ID")
    name: str = Field(..., description="Display name")


class ShareTargetLookupResponse(BaseModel):
    """A looked-up share target; ``target`` is null when no active user uses the email."""

    target: ShareTarget | None = Field(None, description="The matched user, or null")


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
    """Share a job with another user. Only the job owner may do so."""
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
    """List the shares of a job. Only the job owner may do so."""
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
    """Revoke a job share. Only the job owner may do so."""
    await set_current_user(session, user.id)

    await revoke_share_for_owned_job(
        session,
        user.id,
        share_id=share_id,
        not_owned_detail="You can only revoke shares for jobs you own",
    )


@router.get(
    "/lookup",
    response_model=ShareTargetLookupResponse,
    summary="Look up a share target by exact email",
)
async def lookup_share_target(
    email: str = Query(
        ...,
        description="Exact email address of the user to share with",
        examples=["alice@example.com"],
    ),
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> ShareTargetLookupResponse:
    """Resolve one active user by exact email. ``target`` is null when no active
    user uses the email or the email is malformed."""
    await set_current_user(session, user.id)

    # Same approval gate the jobs INSERT policy enforces.
    if not await session.scalar(text("SELECT user_can_start_jobs(:id)"), {"id": user.id}):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account is pending administrator approval. You cannot look up share targets yet.",
        )

    target = await find_share_target_by_email(
        email,
        admin_session_factory=get_admin_session,
    )

    return ShareTargetLookupResponse(
        target=ShareTarget(id=str(target.id), name=target.name) if target else None
    )
