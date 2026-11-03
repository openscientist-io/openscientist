"""API key authentication for the REST API (Bearer ``name:secret`` tokens).

The name is stored in plaintext, the secret as a SHA-256 hash, and the full key is shown once.
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

from openscientist.database.models import APIKey, User
from openscientist.database.session import get_admin_session

logger = logging.getLogger(__name__)

security = HTTPBearer()
AUTH_CREDENTIALS_DEP = Depends(security)


def hash_secret(secret: str) -> str:
    """Hash an API key secret with SHA-256.

    A fast hash is safe because the secret is a high-entropy random token, not a user password.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_secret(secret: str, key_hash: str) -> bool:
    """Verify a secret against its stored hash with a constant-time compare."""
    candidate_hash = hash_secret(secret)
    return hmac.compare_digest(candidate_hash, key_hash)


def generate_api_key_secret(length: int = 32) -> str:
    """Generate a cryptographically random API key secret, hex-encoded.

    ``length`` is the number of random bytes, so the hex string is twice as long.
    """
    return secrets.token_hex(length)


async def get_current_user_from_api_key(
    credentials: HTTPAuthorizationCredentials = AUTH_CREDENTIALS_DEP,
) -> User:
    """Validate the ``name:secret`` Bearer token and return the active user."""
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
        # Resolve by secret hash so the lookup stays deterministic when names collide.
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
            # Not critical. Roll back so the session stays usable for the user lookup.
            await session.rollback()
            logger.warning("Failed to update API key usage stats: %s", e)

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
    """Get an API key by ID, scoped to the calling user."""
    stmt = select(APIKey).where(
        APIKey.id == key_id,
        APIKey.user_id == user.id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
