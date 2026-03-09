"""
FastAPI router for OAuth authentication callbacks.

This module provides FastAPI routes for OAuth login and callback handling,
which integrate with NiceGUI's underlying FastAPI application.
"""

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from openscientist.auth.oauth import get_oauth_client
from openscientist.auth.providers import GitHubProvider, GoogleProvider, MockProvider
from openscientist.auth.routes import create_or_update_user, create_session
from openscientist.database.models import Administrator, ReviewToken, Session, User
from openscientist.database.session import get_admin_session
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

# Auth routes are internal (OAuth callbacks, etc.) - exclude from API docs
router = APIRouter(prefix="/auth", tags=["authentication"], include_in_schema=False)


def _set_session_cookie(response: RedirectResponse, session_id: str) -> None:
    """Set the session cookie on a response."""
    settings = get_settings()
    response.set_cookie(
        key="session_token",
        value=session_id,
        max_age=settings.auth.session_duration_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.auth.app_url.startswith("https"),
        samesite="lax",
    )


# =============================================================================
# Mock Authentication Routes (must come BEFORE parameterized routes)
# =============================================================================


@router.get("/mock/login")
async def mock_oauth_login() -> RedirectResponse:
    """
    Mock OAuth login (development only).

    Automatically creates a user with random credentials and logs them in.
    No form needed - just click and you're in.

    Security Warning: Never enable this in production!
    """
    settings = get_settings()
    if not settings.dev.dev_mode:
        raise HTTPException(status_code=404, detail="Mock auth not enabled")

    # Generate random user credentials
    random_id = uuid.uuid4().hex[:8]
    email = f"dev-{random_id}@example.com"
    name = f"Dev User {random_id}"
    username = f"devuser{random_id}"

    # Create user info using MockProvider
    user_info = await MockProvider.get_user_info(
        {
            "email": email,
            "name": name,
            "username": username,
        }
    )

    # Create/update user and create session
    # Uses admin session because user creation is a cross-tenant operation
    async with get_admin_session() as db:
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
    _set_session_cookie(response, str(session.id))

    logger.info("User %s logged in via mock auth (DEV MODE)", user.email)
    return response


@router.get("/mock/admin-login")
async def mock_admin_oauth_login() -> RedirectResponse:
    """
    Mock OAuth login for admin user (development only).

    Creates/updates a fixed admin user with email admin@mock.local
    and grants admin privileges.

    Security Warning: Never enable this in production!
    """
    settings = get_settings()
    if not settings.dev.dev_mode:
        raise HTTPException(status_code=404, detail="Mock auth not enabled")

    # Fixed admin credentials for persistence across logins
    email = "admin@mock.local"
    name = "Mock Admin"
    username = "mockadmin"

    # Create user info using MockProvider
    user_info = await MockProvider.get_user_info(
        {
            "email": email,
            "name": name,
            "username": username,
        }
    )

    # Create/update user and create session
    # Uses admin session because user creation is a cross-tenant operation
    async with get_admin_session() as db:
        user = await create_or_update_user(
            db,
            provider="mock",
            provider_user_id=user_info["provider_user_id"],
            email=user_info["email"],
            name=user_info["name"],
            access_token="mock_access_token",
            refresh_token=None,
        )
        user.is_approved = True
        user_id = user.id
        user_email = user.email
        login_session = await create_session(db, str(user_id))
        # Extract session ID before exiting context to avoid MissingGreenlet
        session_token = str(login_session.id)

    # Ensure Administrator record exists (use admin session to bypass RLS)
    async with get_admin_session() as db:
        stmt = select(Administrator).where(Administrator.user_id == user_id)
        result = await db.execute(stmt)
        existing_admin = result.scalar_one_or_none()

        if not existing_admin:
            admin_record = Administrator(
                user_id=user_id,
                notes="Auto-created via mock admin login",
            )
            db.add(admin_record)
            await db.commit()
            logger.info("Created admin record for user %s", user_email)

    # Create redirect response with session cookie
    response = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(response, session_token)

    logger.info("Admin user %s logged in via mock auth (DEV MODE)", user_email)
    return response


@router.post("/mock/callback")
async def mock_oauth_callback(request: Request) -> RedirectResponse:
    """
    Handle mock OAuth callback (development only).

    Accepts form data with email, name, and username to create a mock user.

    Security Warning: Never enable this in production!
    """
    settings = get_settings()
    if not settings.dev.dev_mode:
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
        # Uses admin session because user creation is a cross-tenant operation
        async with get_admin_session() as db:
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
        _set_session_cookie(response, str(session.id))

        logger.info("User %s logged in via mock auth (DEV MODE)", user.email)
        return response

    except Exception as e:
        logger.error("Mock OAuth callback error: %s", e, exc_info=True)
        return RedirectResponse(url="/login?error=mock_auth_failed", status_code=303)


# =============================================================================
# OAuth Routes (parameterized - must come AFTER specific routes)
# =============================================================================


@router.get("/{provider}/login")
async def oauth_login(provider: str, request: Request) -> RedirectResponse:
    """
    Initiate OAuth login flow.

    Args:
        provider: OAuth provider name (google, github)
        request: FastAPI request object
    """
    if provider not in ["google", "github"]:
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
        return await client.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]

    except Exception as e:
        logger.error("OAuth login error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="OAuth login failed") from e


@router.get("/{provider}/callback")
async def oauth_callback(provider: str, request: Request) -> RedirectResponse:
    """
    Handle OAuth callback.

    Args:
        provider: OAuth provider name (google, github)
        request: FastAPI request object
    """
    if provider not in ["google", "github"]:
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
        if provider == "google":
            user_info = await GoogleProvider.get_user_info(token)
        elif provider == "github":
            user_info = await GitHubProvider.get_user_info(token)
        else:
            raise ValueError(f"Unknown provider: {provider}")

        # Create/update user and create session
        # Uses admin session because user creation is a cross-tenant operation
        async with get_admin_session() as db:
            user = await create_or_update_user(
                db,
                provider=provider,
                provider_user_id=user_info["provider_user_id"],
                email=user_info["email"],
                name=user_info["name"],
                access_token=token["access_token"],
                refresh_token=token.get("refresh_token"),
                email_verified=user_info.get("email_verified"),
                auth_provider=provider,
            )
            session = await create_session(db, str(user.id))

        # Create redirect response with session cookie
        response = RedirectResponse(url="/")
        _set_session_cookie(response, str(session.id))

        logger.info("User %s logged in via %s", user.email, provider)
        return response

    except Exception as e:
        logger.error("OAuth callback error: %s", e, exc_info=True)
        return RedirectResponse(url="/login?error=oauth_failed")


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Handle logout by invalidating session."""
    session_token = request.cookies.get("session_token")

    if session_token:
        # Invalidate session in database
        try:
            async with get_admin_session() as db:
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


# =============================================================================
# Review Token Routes
# =============================================================================


def _hash_token(plaintext: str) -> str:
    """Hash a review token with SHA-256."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_review_token() -> str:
    """Generate a cryptographically random review token."""
    return secrets.token_urlsafe(32)


async def redeem_review_token(token: str) -> RedirectResponse:
    """
    Redeem a review token to create/login an anonymous reviewer.

    On first use: creates an anonymous User and marks the token as redeemed.
    On subsequent uses: creates a new session for the existing anonymous user.
    """
    token_hash = _hash_token(token)

    async with get_admin_session() as db:
        stmt = select(ReviewToken).where(ReviewToken.token_hash == token_hash).with_for_update()
        result = await db.execute(stmt)
        review_token = result.scalar_one_or_none()

        if review_token is None:
            logger.warning("Review token redemption failed: invalid token")
            return RedirectResponse(url="/login?error=token_invalid", status_code=303)

        if not review_token.is_active:
            logger.warning("Review token redemption failed: token revoked (id=%s)", review_token.id)
            return RedirectResponse(url="/login?error=token_invalid", status_code=303)

        if review_token.is_expired:
            logger.warning("Review token redemption failed: token expired (id=%s)", review_token.id)
            return RedirectResponse(url="/login?error=token_invalid", status_code=303)

        if review_token.redeemed_by_id is not None:
            # Re-login: create new session for existing anonymous user
            user_stmt = select(User).where(User.id == review_token.redeemed_by_id)
            user_result = await db.execute(user_stmt)
            user = user_result.scalar_one_or_none()

            if user is None:
                logger.error(
                    "Review token redeemed_by user not found (id=%s)", review_token.redeemed_by_id
                )
                return RedirectResponse(url="/login?error=token_invalid", status_code=303)

            session = await create_session(db, str(user.id))
            response = RedirectResponse(url="/", status_code=303)
            response.headers["Referrer-Policy"] = "no-referrer"
            _set_session_cookie(response, str(session.id))
            logger.info(
                "Reviewer re-login via token (token_id=%s, user_id=%s)",
                review_token.id,
                user.id,
            )
            return response

        # First redemption: create anonymous user
        random_hex = secrets.token_hex(8)
        anon_email = f"reviewer-{random_hex}@review.local"
        anon_user = User(
            email=anon_email,
            name=review_token.label,
            is_approved=True,
        )
        db.add(anon_user)
        await db.flush()

        review_token.redeemed_at = datetime.now(UTC)
        review_token.redeemed_by_id = anon_user.id

        session = await create_session(db, str(anon_user.id))

        response = RedirectResponse(url="/", status_code=303)
        response.headers["Referrer-Policy"] = "no-referrer"
        _set_session_cookie(response, str(session.id))

        logger.info(
            "Reviewer created via token (token_id=%s, user_id=%s, email=%s)",
            review_token.id,
            anon_user.id,
            anon_email,
        )
        return response
