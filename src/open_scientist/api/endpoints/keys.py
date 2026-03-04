"""
API key management endpoints.

Provides REST API endpoints for creating, listing, and revoking API keys.
"""

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.api.auth import (
    generate_api_key_secret,
    get_api_key_by_id,
    get_current_user_from_api_key,
    hash_secret,
)
from shandy.database.models import APIKey, User
from shandy.database.rls import set_current_user
from shandy.database.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/keys", tags=["API Keys"])
CURRENT_USER_DEP = Depends(get_current_user_from_api_key)
SESSION_DEP = Depends(get_session)


# Pydantic models for request/response
class APIKeyCreate(BaseModel):
    """Request body for creating an API key."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Descriptive name for the API key (e.g., 'jenkins-ci', 'local-dev')",
        examples=["jenkins-ci"],
    )


class APIKeyResponse(BaseModel):
    """Response for a created or listed API key."""

    id: str = Field(..., description="API key ID")
    name: str = Field(..., description="API key name")
    created_at: datetime = Field(..., description="Creation timestamp (UTC)")
    last_used_at: datetime | None = Field(None, description="Last used timestamp (UTC)")
    is_active: bool = Field(..., description="Whether key is active")


class APIKeyCreateResponse(APIKeyResponse):
    """Response for newly created API key (includes full key once)."""

    key: str = Field(
        ...,
        description="Full API key in format 'name:secret' - ONLY SHOWN ONCE!",
        examples=["my-key:a1b2c3d4e5f6..."],
    )


class APIKeyListResponse(BaseModel):
    """Response for listing API keys."""

    keys: list[APIKeyResponse] = Field(..., description="List of API keys")


@router.post("", response_model=APIKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    key_data: APIKeyCreate,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> APIKeyCreateResponse:
    """
    Create a new API key for the authenticated user.

    The full API key (name:secret) is only returned in this response and cannot
    be retrieved later. Store it securely!

    Rate limit: 10 keys per user maximum.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    # Check if name already exists for this user
    stmt = select(APIKey).where(
        APIKey.user_id == user.id,
        APIKey.name == key_data.name,
    )
    result = await session.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"API key with name '{key_data.name}' already exists",
        )

    # Check key count limit
    count_result = await session.execute(
        select(func.count(APIKey.id)).where(APIKey.user_id == user.id)
    )
    if count_result.scalar_one() >= 10:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Maximum of 10 API keys per user",
        )

    # Generate secret and hash it
    secret = generate_api_key_secret()
    key_hash = hash_secret(secret)

    # Create API key
    api_key = APIKey(
        user_id=user.id,
        name=key_data.name,
        key_hash=key_hash,
        is_active=True,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)

    logger.info("Created API key '%s' for user %s", key_data.name, user.email)

    # Return full key (this is the only time it's shown)
    return APIKeyCreateResponse(
        id=str(api_key.id),
        name=api_key.name,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        is_active=api_key.is_active,
        key=f"{key_data.name}:{secret}",
    )


@router.get("", response_model=APIKeyListResponse)
async def list_api_keys(
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> APIKeyListResponse:
    """
    List all API keys for the authenticated user.

    Note: The secret portion of keys is not included (it's only shown once at creation).
    """
    # Set RLS context
    await set_current_user(session, user.id)

    stmt = select(APIKey).where(APIKey.user_id == user.id).order_by(APIKey.created_at.desc())
    result = await session.execute(stmt)
    keys = result.scalars().all()

    return APIKeyListResponse(
        keys=[
            APIKeyResponse(
                id=str(key.id),
                name=key.name,
                created_at=key.created_at,
                last_used_at=key.last_used_at,
                is_active=key.is_active,
            )
            for key in keys
        ]
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: UUID,
    user: User = CURRENT_USER_DEP,
    session: AsyncSession = SESSION_DEP,
) -> None:
    """
    Revoke (deactivate) an API key.

    Revoked keys cannot be reactivated - create a new key instead.
    """
    # Set RLS context
    await set_current_user(session, user.id)

    api_key = await get_api_key_by_id(key_id, user, session)

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found",
        )

    # Deactivate the key
    api_key.is_active = False
    await session.commit()

    logger.info("Revoked API key '%s' for user %s", api_key.name, user.email)
