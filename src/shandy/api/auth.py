"""
API key authentication for REST API.

Implements Bearer token authentication using API keys in the format:
    name:secret

The name is stored plaintext in the database, the secret is hashed using
bcrypt for secure storage. The full "name:secret" key is only shown once
at creation time.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import APIKey, User
from shandy.database.session import get_admin_session

logger = logging.getLogger(__name__)

# Use HTTPBearer for extracting Bearer tokens from Authorization header
security = HTTPBearer()
AUTH_CREDENTIALS_DEP = Depends(security)


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
    candidate_hash = hash_secret(secret)
    return hmac.compare_digest(candidate_hash, key_hash)


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
    credentials: HTTPAuthorizationCredentials = AUTH_CREDENTIALS_DEP,
) -> User:
    """
    Dependency to extract and validate API key from Authorization header.

    Expected format:
        Authorization: Bearer <name>:<secret>

    Args:
        credentials: HTTP bearer credentials injected by FastAPI.

    Returns:
        Authenticated and active user row.

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

    secret_hash = hash_secret(secret)

    async with get_admin_session() as session:
        # Resolve API key by secret hash so lookup is deterministic even when names collide.
        key_stmt = select(APIKey).where(
            APIKey.key_hash == secret_hash,
            APIKey.is_active.is_(True),
        )
        key_result = await session.execute(key_stmt)
        api_key = key_result.scalar_one_or_none()

        if not api_key:
            logger.warning("API key not found or inactive for name: %s", name)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Keep the name component as part of the credential contract.
        if not hmac.compare_digest(name, api_key.name):
            logger.warning("API key name mismatch")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Double-check hash using constant-time compare for defense in depth.
        if not verify_secret(secret, api_key.key_hash):
            logger.warning("API key secret mismatch for: %s", name)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Update last_used_at timestamp and increment usage count
        try:
            update_stmt = (
                update(APIKey)
                .where(APIKey.id == api_key.id)
                .values(
                    last_used_at=datetime.now(UTC),
                    usage_count=APIKey.usage_count + 1,
                )
            )
            await session.execute(update_stmt)
            await session.commit()
        except Exception as e:
            # Non-critical, log and continue
            logger.warning("Failed to update API key usage stats: %s", e)

        # Load user
        user_stmt = select(User).where(User.id == api_key.user_id, User.is_active.is_(True))
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
) -> APIKey | None:
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
