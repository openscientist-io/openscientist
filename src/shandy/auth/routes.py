"""
Authentication routes for OAuth login/logout.

Handles OAuth callback flows and session creation for authenticated users.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID as _UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shandy.database.models import OAuthAccount, Session, User
from shandy.settings import get_settings

logger = logging.getLogger(__name__)


async def create_or_update_user(
    db: AsyncSession,
    provider: str,
    provider_user_id: str,
    email: str,
    name: str,
    access_token: str,
    refresh_token: Optional[str] = None,
) -> User:
    """
    Create or update user from OAuth data.

    If an OAuth account exists, update its tokens and return the associated user.
    If not, create a new user and OAuth account link.

    Args:
        db: Database session
        provider: OAuth provider name (github, google)
        provider_user_id: User's ID on the provider
        email: User's email
        name: User's display name
        access_token: OAuth access token
        refresh_token: OAuth refresh token (optional)

    Returns:
        User object (created or existing)
    """
    # Check if OAuth account exists (eager load user to avoid lazy load in async)
    oauth_stmt = (
        select(OAuthAccount)
        .options(selectinload(OAuthAccount.user))
        .where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )
    oauth_result = await db.execute(oauth_stmt)
    oauth_account = oauth_result.scalar_one_or_none()

    if oauth_account:
        # Update existing OAuth account
        oauth_account.access_token = access_token
        oauth_account.refresh_token = refresh_token
        oauth_account.email = email
        oauth_account.name = name
        await db.flush()
        user = oauth_account.user
        logger.info("Updated OAuth account for user %s (provider=%s)", user.id, provider)
    else:
        # Check if user exists with this email
        user_stmt = select(User).where(User.email == email)
        user_result = await db.execute(user_stmt)
        existing_user = user_result.scalar_one_or_none()

        if not existing_user:
            # Create new user
            user = User(
                email=email,
                name=name,
            )
            db.add(user)
            await db.flush()  # Get user.id
            logger.info("Created new user: %s (%s)", user.email, user.id)
        else:
            user = existing_user

        # Create OAuth account link
        oauth_account = OAuthAccount(
            user_id=user.id,
            provider=provider,
            provider_user_id=provider_user_id,
            email=email,
            name=name,
            access_token=access_token,
            refresh_token=refresh_token,
        )
        db.add(oauth_account)
        logger.info("Linked %s OAuth account to user %s", provider, user.id)

    await db.commit()
    await db.refresh(user)
    return user


async def create_session(db: AsyncSession, user_id: str) -> Session:
    """
    Create a new session for a user.

    Args:
        db: Database session
        user_id: User ID (as string, will be converted to UUID)

    Returns:
        New Session object
    """
    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.auth.session_duration_days)

    session = Session(
        user_id=_UUID(user_id) if isinstance(user_id, str) else user_id,
        expires_at=expires_at,
    )
    db.add(session)
    await db.commit()

    logger.info("Created session for user %s, expires %s", user_id, expires_at)
    return session
