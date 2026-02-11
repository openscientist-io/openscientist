"""
FastAPI router for OAuth authentication callbacks.

This module provides FastAPI routes for OAuth login and callback handling,
which integrate with NiceGUI's underlying FastAPI application.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.auth.oauth import get_oauth_client
from shandy.auth.providers import GitHubProvider, MockProvider, ORCIDProvider
from shandy.database.models import OAuthAccount, Session, User
from shandy.database.session import get_session
from shandy.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])


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
        user_id: User ID

    Returns:
        New Session object
    """
    settings = get_settings()
    expires_at = datetime.utcnow() + timedelta(days=settings.auth.session_duration_days)

    session = Session(
        user_id=user_id,
        expires_at=expires_at,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    logger.info("Created session for user %s, expires %s", user_id, expires_at)
    return session


@router.get("/{provider}/login")
async def oauth_login(provider: str, request: Request):
    """
    Initiate OAuth login flow.

    Args:
        provider: OAuth provider name (github, orcid)
        request: FastAPI request object
    """
    if provider not in ["github", "orcid"]:
        raise HTTPException(status_code=400, detail="Unknown OAuth provider")

    try:
        oauth = get_oauth_client()
        client = getattr(oauth, provider, None)

        if not client:
            raise HTTPException(
                status_code=400, detail=f"{provider.title()} OAuth is not configured"
            )

        # Build redirect URI
        settings = get_settings()
        redirect_uri = f"{settings.auth.app_url}/auth/{provider}/callback"

        # Redirect to OAuth provider
        return await client.authorize_redirect(request, redirect_uri)

    except Exception as e:
        logger.error("OAuth login error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="OAuth login failed")


@router.get("/{provider}/callback")
async def oauth_callback(provider: str, request: Request):
    """
    Handle OAuth callback.

    Args:
        provider: OAuth provider name (github, orcid)
        request: FastAPI request object
    """
    if provider not in ["github", "orcid"]:
        raise HTTPException(status_code=400, detail="Unknown OAuth provider")

    try:
        oauth = get_oauth_client()
        client = getattr(oauth, provider, None)

        if not client:
            raise HTTPException(
                status_code=400, detail=f"{provider.title()} OAuth is not configured"
            )

        # Get OAuth token
        token = await client.authorize_access_token(request)

        # Get user info from provider
        if provider == "github":
            user_info = await GitHubProvider.get_user_info(token)
        elif provider == "orcid":
            user_info = await ORCIDProvider.get_user_info(token)
        else:
            raise ValueError(f"Unknown provider: {provider}")

        # Create/update user and create session
        async with get_session() as db:
            user = await create_or_update_user(
                db,
                provider=provider,
                provider_user_id=user_info["provider_user_id"],
                email=user_info["email"],
                name=user_info["name"],
                access_token=token["access_token"],
                refresh_token=token.get("refresh_token"),
            )
            session = await create_session(db, str(user.id))

        # Create redirect response with session cookie
        response = RedirectResponse(url="/")

        # Set session cookie (HttpOnly for security)
        settings = get_settings()
        response.set_cookie(
            key="session_token",
            value=str(session.id),
            max_age=settings.auth.session_duration_days * 24 * 60 * 60,  # seconds
            httponly=True,
            secure=settings.auth.app_url.startswith("https"),  # Secure cookie for HTTPS
            samesite="lax",
        )

        logger.info("User %s logged in via %s", user.email, provider)
        return response

    except Exception as e:
        logger.error("OAuth callback error: %s", e, exc_info=True)
        return RedirectResponse(url="/login?error=oauth_failed")


@router.get("/logout")
async def logout(request: Request):
    """Handle logout by invalidating session."""
    session_token = request.cookies.get("session_token")

    if session_token:
        # Invalidate session in database
        try:
            async with get_session() as db:
                stmt = select(Session).where(Session.id == session_token)
                result = await db.execute(stmt)
                session = result.scalar_one_or_none()

                if session:
                    await db.delete(session)
                    await db.commit()
                    logger.info("Deleted session: %s", session_token)
        except Exception as e:
            logger.error("Error deleting session: %s", e)

    # Clear session cookie and redirect
    response = RedirectResponse(url="/login")
    response.delete_cookie(key="session_token")
    return response


@router.get("/mock/login")
async def mock_oauth_login():
    """
    Initiate mock OAuth login flow (development only).

    This endpoint is only available when ENABLE_MOCK_AUTH is set.
    It redirects to a simple form where users can enter their test credentials.

    Security Warning: Never enable this in production!
    """
    settings = get_settings()
    if not settings.auth.enable_mock_auth:
        raise HTTPException(status_code=404, detail="Mock auth not enabled")

    # Redirect to mock login form (handled by NiceGUI)
    return RedirectResponse(url="/mock-login-form")


@router.post("/mock/callback")
async def mock_oauth_callback(request: Request):
    """
    Handle mock OAuth callback (development only).

    Accepts form data with email, name, and username to create a mock user.

    Security Warning: Never enable this in production!
    """
    settings = get_settings()
    if not settings.auth.enable_mock_auth:
        raise HTTPException(status_code=404, detail="Mock auth not enabled")

    try:
        # Get form data
        form_data = await request.form()
        email = str(form_data.get("email", "dev@example.com"))
        name = str(form_data.get("name", "Dev User"))
        username = str(form_data.get("username", email.split("@")[0]))

        # Prepare user info using MockProvider
        user_info = await MockProvider.get_user_info(
            {
                "email": email,
                "name": name,
                "username": username,
            }
        )

        # Create/update user and create session
        async with get_session() as db:
            user = await create_or_update_user(
                db,
                provider="mock",
                provider_user_id=user_info["provider_user_id"],
                email=user_info["email"],
                name=user_info["name"],
                access_token="mock_access_token",
                refresh_token=None,
            )
            session = await create_session(db, str(user.id))

        # Create redirect response with session cookie
        response = RedirectResponse(url="/", status_code=303)

        # Set session cookie (HttpOnly for security)
        response.set_cookie(
            key="session_token",
            value=str(session.id),
            max_age=settings.auth.session_duration_days * 24 * 60 * 60,  # seconds
            httponly=True,
            secure=settings.auth.app_url.startswith("https"),  # Secure cookie for HTTPS
            samesite="lax",
        )

        logger.info("User %s logged in via mock auth (DEV MODE)", user.email)
        return response

    except Exception as e:
        logger.error("Mock OAuth callback error: %s", e, exc_info=True)
        return RedirectResponse(url="/login?error=mock_auth_failed", status_code=303)
