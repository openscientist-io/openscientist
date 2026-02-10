"""
API key authentication for REST API.

Implements Bearer token authentication using API keys in the format:
    name:secret

The name is stored plaintext in the database, the secret is hashed using
bcrypt for secure storage. The full "name:secret" key is only shown once
at creation time.
"""

import hashlib
import logging
import secrets
from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import APIKey, User
from shandy.database.session import get_session

logger = logging.getLogger(__name__)

# Use HTTPBearer for extracting Bearer tokens from Authorization header
security = HTTPBearer()


def hash_secret(secret: str) -> str:
    """
    Hash an API key secret using SHA-256.

    Note: We use SHA-256 instead of bcrypt for API keys because:
    1. API keys are high-entropy tokens (not user passwords)
    2. SHA-256 is faster for API request validation
    3. The secret component is cryptographically random

    Args:
        secret: The secret portion of the API key

    Returns:
        Hexadecimal hash of the secret
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_secret(secret: str, key_hash: str) -> bool:
    """
    Verify a secret against its stored hash.

    Args:
        secret: The secret to verify
        key_hash: The stored hash

    Returns:
        True if the secret matches, False otherwise
    """
    return hash_secret(secret) == key_hash


def generate_api_key_secret(length: int = 32) -> str:
    """
    Generate a cryptographically secure random API key secret.

    Args:
        length: Number of random bytes (will be hex-encoded, so final length is 2x)

    Returns:
        Hex-encoded random string
    """
    return secrets.token_hex(length)


async def get_current_user_from_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: AsyncSession = Depends(get_session),
) -> User:
    """
    Dependency to extract and validate API key from Authorization header.

    Expected format:
        Authorization: Bearer <name>:<secret>

    Args:
        credentials: HTTP Authorization credentials (injected by FastAPI)
        session: Database session (injected by FastAPI)

    Returns:
        User object if authentication succeeds

    Raises:
        HTTPException: If authentication fails (401 Unauthorized)
    """
    # Parse API key in format "name:secret"
    api_key_full = credentials.credentials
    if ":" not in api_key_full:
        logger.warning("Invalid API key format (missing colon)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key format. Expected: name:secret",
            headers={"WWW-Authenticate": "Bearer"},
        )

    name, secret = api_key_full.split(":", 1)

    # Look up API key by name
    key_stmt = (
        select(APIKey)
        .where(APIKey.name == name, APIKey.is_active == True)  # noqa: E712
        .limit(1)
    )
    key_result = await session.execute(key_stmt)
    api_key = key_result.scalar_one_or_none()

    if not api_key:
        logger.warning("API key not found or inactive: %s", name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify secret
    if not verify_secret(secret, api_key.key_hash):
        logger.warning("API key secret mismatch for: %s", name)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Update last_used_at timestamp
    try:
        update_stmt = (
            update(APIKey).where(APIKey.id == api_key.id).values(last_used_at=datetime.utcnow())
        )
        await session.execute(update_stmt)
        await session.commit()
    except Exception as e:
        # Non-critical, log and continue
        logger.warning("Failed to update API key last_used_at: %s", e)

    # Load user
    user_stmt = select(User).where(User.id == api_key.user_id)
    user_result = await session.execute(user_stmt)
    user = user_result.scalar_one_or_none()

    if not user:
        logger.error("User not found for API key: %s (user_id=%s)", name, api_key.user_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_api_key_by_id(
    key_id: UUID,
    user: User,
    session: AsyncSession,
) -> Optional[APIKey]:
    """
    Get an API key by ID, verifying it belongs to the user.

    Args:
        key_id: API key ID
        user: Current authenticated user
        session: Database session

    Returns:
        APIKey object if found and owned by user, None otherwise
    """
    stmt = select(APIKey).where(
        APIKey.id == key_id,
        APIKey.user_id == user.id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
