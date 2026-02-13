"""
Authentication routes for OAuth login/logout.

Handles OAuth callback flows and session creation for authenticated users.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from nicegui import app, ui
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.auth.oauth import get_oauth_client, is_oauth_configured
from shandy.database.models import OAuthAccount, Session, User
from shandy.database.session import get_session
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
        provider: OAuth provider name (github, orcid)
        provider_user_id: User's ID on the provider
        email: User's email
        name: User's display name
        access_token: OAuth access token
        refresh_token: OAuth refresh token (optional)

    Returns:
        User object (created or existing)
    """
    # Check if OAuth account exists
    oauth_stmt = select(OAuthAccount).where(
        OAuthAccount.provider == provider,
        OAuthAccount.provider_user_id == provider_user_id,
    )
    oauth_result = await db.execute(oauth_stmt)
    oauth_account = oauth_result.scalar_one_or_none()

    if oauth_account:
        # Update existing OAuth account
        oauth_account.access_token = access_token
        oauth_account.refresh_token = refresh_token
        oauth_account.email = email
        oauth_account.name = name
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
    from uuid import UUID as _UUID

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


@ui.page("/auth/{provider}/login")
async def oauth_login(provider: str):
    """
    Initiate OAuth login flow.

    Args:
        provider: OAuth provider name (github, orcid)
    """
    if not is_oauth_configured():
        ui.notify("OAuth is not configured", color="negative")
        ui.navigate.to("/login")
        return

    oauth = get_oauth_client()

    if provider not in ["github", "orcid"]:
        ui.notify("Unknown OAuth provider", color="negative")
        ui.navigate.to("/login")
        return

    try:
        # Get the OAuth client for the provider
        client = getattr(oauth, provider, None)
        if not client:
            ui.notify(f"{provider.title()} OAuth is not configured", color="negative")
            ui.navigate.to("/login")
            return

        # Store return URL in session
        return_to = app.storage.user.get("return_to", "/")
        app.storage.user["oauth_return_to"] = return_to

        # Redirect to OAuth provider
        # Note: This is a placeholder - actual implementation depends on
        # NiceGUI's request handling. May need to use Starlette directly.
        settings = get_settings()
        redirect_uri = f"{settings.auth.app_url}/auth/{provider}/callback"

        # For NiceGUI, we'll need to handle this differently
        # This is a template for the actual implementation
        ui.markdown(f"Redirecting to {provider.title()}...")
        ui.markdown(
            f"**Manual step needed:** Navigate to OAuth flow with redirect_uri={redirect_uri}"
        )

    except Exception as e:
        logger.error("OAuth login error: %s", e, exc_info=True)
        ui.notify("Login failed", color="negative")
        ui.navigate.to("/login")


@ui.page("/auth/{provider}/callback")
async def oauth_callback(provider: str):
    """
    Handle OAuth callback.

    Args:
        provider: OAuth provider name (github, orcid)
    """
    # This is a template - actual implementation will need request access
    # May need to use Starlette/FastAPI directly for OAuth callbacks

    try:
        oauth = get_oauth_client()
        client = getattr(oauth, provider, None)

        if not client:
            raise ValueError(f"Unknown provider: {provider}")

        # Get OAuth token (placeholder - need actual request)
        # token = await client.authorize_access_token(request)

        # Get user info from provider
        if provider == "github":
            # user_info = await GitHubProvider.get_user_info(token)
            pass
        elif provider == "orcid":
            # user_info = await ORCIDProvider.get_user_info(token)
            pass
        else:
            raise ValueError(f"Unknown provider: {provider}")

        # Create/update user and create session
        # async with get_session() as db:
        #     user = await create_or_update_user(
        #         db,
        #         provider=provider,
        #         provider_user_id=user_info["provider_user_id"],
        #         email=user_info["email"],
        #         name=user_info["name"],
        #         access_token=token["access_token"],
        #         refresh_token=token.get("refresh_token"),
        #     )
        #     session = await create_session(db, user.id)

        # Store session in browser
        # app.storage.user["session_token"] = session.session_token
        # app.storage.user["user_id"] = str(user.id)
        # app.storage.user["authenticated"] = True

        # Redirect to original destination
        # return_to = app.storage.user.pop("oauth_return_to", "/")
        # ui.navigate.to(return_to)

        ui.markdown("OAuth callback received - implementation in progress")

    except Exception as e:
        logger.error("OAuth callback error: %s", e, exc_info=True)
        ui.notify("Login failed", color="negative")
        ui.navigate.to("/login")


@ui.page("/logout")
async def logout():
    """Handle logout."""
    session_token = app.storage.user.get("session_token")

    if session_token:
        # Invalidate session in database
        try:
            async with get_session() as db:
                stmt = select(Session).where(Session.session_token == session_token)
                result = await db.execute(stmt)
                session = result.scalar_one_or_none()

                if session:
                    await db.delete(session)
                    await db.commit()
                    logger.info("Deleted session: %s", session_token)
        except Exception as e:
            logger.error("Error deleting session: %s", e)

    # Clear browser storage
    app.storage.user.clear()

    ui.notify("Logged out successfully")
    ui.navigate.to("/login")
