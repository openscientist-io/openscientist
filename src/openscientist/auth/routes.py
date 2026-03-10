"""
Authentication routes for OAuth login/logout.

Handles OAuth callback flows and session creation for authenticated users.
"""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID as _UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openscientist.database.models import Administrator, OAuthAccount, Session, User
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    """Normalize email for exact matching."""
    return email.strip().lower()


async def _maybe_bootstrap_admin(
    db: AsyncSession,
    user: User,
    email: str,
    email_verified: bool | None,
    auth_provider: str | None,
) -> bool:
    """
    Grant bootstrap admin privileges based on BOOTSTRAP_ADMIN_EMAILS.

    Returns:
        True if user is in the bootstrap allowlist (whether newly granted or already admin),
        False otherwise.
    """
    allowlist = get_settings().auth.bootstrap_admin_emails_set
    if not allowlist:
        return False

    normalized_email = _normalize_email(email)
    if normalized_email not in allowlist:
        return False

    if email_verified is not True:
        logger.warning(
            "Bootstrap admin match denied for %s (provider=%s): email not verified",
            normalized_email,
            auth_provider or "unknown",
        )
        return False

    stmt = select(Administrator).where(Administrator.user_id == user.id)
    result = await db.execute(stmt)
    existing_admin = result.scalar_one_or_none()

    # Bootstrap admins are always approved to start jobs.
    user.is_approved = True

    if existing_admin:
        logger.info(
            "Bootstrap admin matched existing admin user %s (%s)",
            user.id,
            normalized_email,
        )
        return True

    notes = (
        "Auto-granted via BOOTSTRAP_ADMIN_EMAILS during OAuth login "
        f"(provider={auth_provider or 'unknown'})"
    )
    db.add(Administrator(user_id=user.id, notes=notes))
    logger.info("Bootstrap admin granted to user %s (%s)", user.id, normalized_email)
    return True


async def create_or_update_user(
    db: AsyncSession,
    provider: str,
    provider_user_id: str,
    email: str,
    name: str,
    access_token: str,
    refresh_token: str | None = None,
    email_verified: bool | None = None,
    auth_provider: str | None = None,
) -> User:
    """
    Create or update user from OAuth data.

    If an OAuth account exists, update its tokens and return the associated user.
    If not, create a new user and OAuth account link.

    Args:
        db: Database session
        provider: OAuth provider name (github, google, orcid)
        provider_user_id: User's ID on the provider
        email: User's email
        name: User's display name
        access_token: OAuth access token
        refresh_token: OAuth refresh token (optional)
        email_verified: Whether provider confirmed email verification
        auth_provider: Provider name for logging bootstrap grants

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

    await _maybe_bootstrap_admin(
        db,
        user=user,
        email=email,
        email_verified=email_verified,
        auth_provider=auth_provider or provider,
    )

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
    expires_at = datetime.now(UTC) + timedelta(days=settings.auth.session_duration_days)

    session = Session(
        user_id=_UUID(user_id) if isinstance(user_id, str) else user_id,
        expires_at=expires_at,
    )
    db.add(session)
    await db.commit()

    logger.info("Created session for user %s, expires %s", user_id, expires_at)
    return session
