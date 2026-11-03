"""FastAPI router for OAuth authentication callbacks."""

import asyncio
import hashlib
import html
import logging
import secrets
import threading
import uuid
from datetime import UTC, datetime
from string import Template

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from nicegui import app
from pydantic import BaseModel
from sqlalchemy import select
from starlette.responses import Response

from openscientist.admin_settings import current_admin_settings
from openscientist.auth.oauth import get_oauth_client
from openscientist.auth.providers import GitHubProvider, GoogleProvider, MockProvider, OrcidProvider
from openscientist.auth.routes import create_or_update_user, create_session
from openscientist.database.models import Administrator, ReviewToken, Session, User
from openscientist.database.retry import is_connection_error, retry_sync
from openscientist.database.session import (
    SessionOpener,
    get_admin_session,
    thread_safe_admin_session,
)
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

# Auth routes are internal (OAuth callbacks, etc.) - exclude from API docs
router = APIRouter(prefix="/auth", tags=["authentication"], include_in_schema=False)


async def _set_session_cookie(response: RedirectResponse, session_id: str) -> None:
    """Set the session cookie on a response."""
    settings = get_settings()
    admin = await current_admin_settings()
    response.set_cookie(
        key="session_token",
        value=session_id,
        max_age=admin.session_duration_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.auth.app_url.startswith("https"),
        samesite="lax",
    )


# Mock routes must be declared before the parameterized provider routes


@router.get("/mock/login")
async def mock_oauth_login() -> RedirectResponse:
    """Mock OAuth login (development only).

    Creates a user with random credentials and logs them in. Never enable in
    production.
    """
    settings = get_settings()
    if not settings.dev.dev_mode:
        raise HTTPException(status_code=404, detail="Mock auth not enabled")

    random_id = uuid.uuid4().hex[:8]
    email = f"dev-{random_id}@example.com"
    name = f"Dev User {random_id}"
    username = f"devuser{random_id}"

    user_info = await MockProvider.get_user_info(
        {
            "email": email,
            "name": name,
            "username": username,
        }
    )

    # Admin session because user creation is a cross-tenant operation
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

    response = RedirectResponse(url="/", status_code=303)
    await _set_session_cookie(response, str(session.id))

    logger.info("User %s logged in via mock auth (DEV MODE)", user.email)
    return response


@router.get("/mock/admin-login")
async def mock_admin_oauth_login() -> RedirectResponse:
    """Mock OAuth login for the admin user (development only).

    Creates or updates a fixed admin@mock.local user and grants admin
    privileges. Never enable in production.
    """
    settings = get_settings()
    if not settings.dev.dev_mode:
        raise HTTPException(status_code=404, detail="Mock auth not enabled")

    # Fixed admin credentials for persistence across logins
    email = "admin@mock.local"
    name = "Mock Admin"
    username = "mockadmin"

    user_info = await MockProvider.get_user_info(
        {
            "email": email,
            "name": name,
            "username": username,
        }
    )

    # Admin session because user creation is a cross-tenant operation
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

    # Ensure the Administrator record exists (admin session bypasses RLS)
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

    response = RedirectResponse(url="/", status_code=303)
    await _set_session_cookie(response, session_token)

    logger.info("Admin user %s logged in via mock auth (DEV MODE)", user_email)
    return response


@router.post("/mock/callback")
async def mock_oauth_callback(request: Request) -> RedirectResponse:
    """Handle mock OAuth callback (development only).

    Accepts form data with email, name, and username. Never enable in production.
    """
    settings = get_settings()
    if not settings.dev.dev_mode:
        raise HTTPException(status_code=404, detail="Mock auth not enabled")

    try:
        form_data = await request.form()
        email = str(form_data.get("email", "dev@example.com"))
        name = str(form_data.get("name", "Dev User"))
        username = str(form_data.get("username", email.split("@")[0]))

        user_info = await MockProvider.get_user_info(
            {
                "email": email,
                "name": name,
                "username": username,
            }
        )

        # Admin session because user creation is a cross-tenant operation
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

        response = RedirectResponse(url="/", status_code=303)
        await _set_session_cookie(response, str(session.id))

        logger.info("User %s logged in via mock auth (DEV MODE)", user.email)
        return response

    except Exception as e:
        logger.error("Mock OAuth callback error: %s", e, exc_info=True)
        return RedirectResponse(url="/login?error=mock_auth_failed", status_code=303)


@router.get("/{provider}/login")
async def oauth_login(provider: str, request: Request) -> RedirectResponse:
    """Initiate the OAuth login flow."""
    if provider not in ["google", "github", "orcid"]:
        raise HTTPException(status_code=400, detail="Unknown OAuth provider")

    try:
        oauth = get_oauth_client()
        client = getattr(oauth, provider, None)

        if not client:
            raise HTTPException(
                status_code=400, detail=f"{provider.title()} OAuth is not configured"
            )

        settings = get_settings()
        redirect_uri = f"{settings.auth.app_url}/auth/{provider}/callback"

        return await client.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]

    except HTTPException:
        raise
    except Exception as e:
        logger.error("OAuth login error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="OAuth login failed") from e


@router.get("/{provider}/callback")
async def oauth_callback(provider: str, request: Request) -> RedirectResponse:
    """Handle the OAuth callback."""
    if provider not in ["google", "github", "orcid"]:
        raise HTTPException(status_code=400, detail="Unknown OAuth provider")

    try:
        oauth = get_oauth_client()
        client = getattr(oauth, provider, None)

        if not client:
            raise HTTPException(
                status_code=400, detail=f"{provider.title()} OAuth is not configured"
            )

        token = await client.authorize_access_token(request)

        if provider == "google":
            user_info = await GoogleProvider.get_user_info(token)
        elif provider == "github":
            user_info = await GitHubProvider.get_user_info(token)
        elif provider == "orcid":
            user_info = await OrcidProvider.get_user_info(token)
        else:
            raise ValueError(f"Unknown provider: {provider}")

        # Admin session because user creation is a cross-tenant operation
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

        response = RedirectResponse(url="/")
        await _set_session_cookie(response, str(session.id))

        logger.info("User %s logged in via %s", user.email, provider)
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error("OAuth callback error: %s", e, exc_info=True)
        return RedirectResponse(url="/login?error=oauth_failed")


async def _delete_session(open_session: SessionOpener, session_token: str) -> None:
    async with open_session() as db:
        session = (
            await db.execute(select(Session).where(Session.id == session_token))
        ).scalar_one_or_none()
        if session:
            await db.delete(session)
            await db.commit()


def _revoke_session_later(session_token: str) -> None:
    """Delete the session once the database is back, so the token dies with
    the sign-out even when the database was unreachable at that moment."""

    def revoke() -> None:
        try:
            retry_sync(
                lambda: asyncio.run(_delete_session(thread_safe_admin_session, session_token)),
                what="revoking a signed-out session",
            )
        except Exception:
            logger.exception("Could not revoke a signed-out session")
            return
        logger.info("Revoked a session signed out while the database was unreachable")

    threading.Thread(target=revoke, name="session-revoke", daemon=True).start()


def _forget_stored_session() -> None:
    """Clear the per-browser user info the page build stored."""
    try:
        storage = app.storage.user
    except RuntimeError:
        return  # outside a NiceGUI request there is no stored copy
    storage.clear()


class LogoutRequest(BaseModel):
    """JSON-only body that keeps cross-site forms from forcing a sign-out."""


@router.post("/logout")
async def logout(request: Request, _logout: LogoutRequest) -> RedirectResponse:
    """Handle logout by invalidating session."""
    session_token = request.cookies.get("session_token")

    if session_token:
        try:
            await _delete_session(get_admin_session, session_token)
        except Exception as e:
            if not is_connection_error(e):
                raise
            logger.error("Database unreachable at sign-out, revoking the session later: %s", e)
            _revoke_session_later(session_token)
    _forget_stored_session()

    response = RedirectResponse(url="/login")
    response.delete_cookie(key="session_token")
    # The "session" cookie is NiceGUI's own; it keys the per-browser storage
    response.delete_cookie(key="session")
    return response


def _hash_token(plaintext: str) -> str:
    """Hash a review token with SHA-256."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_review_token() -> str:
    """Generate a cryptographically random review token."""
    return secrets.token_urlsafe(32)


class RedeemRequest(BaseModel):
    """JSON-only body that keeps cross-site forms from minting a reviewer."""


_REVIEW_CONFIRMATION_PAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenScientist - Reviewer sign-in</title>
<style>
body {
    font-family: system-ui, -apple-system, sans-serif;
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    margin: 0;
    background: #f5f6fa;
    color: #333;
}
.card {
    background: #fff;
    border-radius: 12px;
    box-shadow: 0 2px 12px rgba(0, 0, 0, 0.12);
    padding: 2.5rem;
    max-width: 24rem;
    text-align: center;
}
h1 { font-size: 1.25rem; margin: 0 0 0.75rem; }
p { margin: 0 0 1.5rem; color: #666; }
button {
    background: #1976d2;
    color: #fff;
    border: 0;
    border-radius: 8px;
    padding: 0.6rem 1.8rem;
    font-size: 1rem;
    cursor: pointer;
}
</style>
</head>
<body>
<div class="card">
<h1>Sign in as $label?</h1>
<p>This link signs you in to an OpenScientist review account.</p>
<button id="sign-in">Sign in</button>
<noscript><p>JavaScript is required to sign in from this link.</p></noscript>
</div>
<script>
document.getElementById("sign-in").addEventListener("click", async () => {
    const response = await fetch(window.location.pathname, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: "{}",
    });
    window.location.href = response.url.includes("/login") ? "/login?error=token_invalid" : "/";
});
</script>
</body>
</html>
"""
)


async def review_confirmation_page(token: str) -> Response:
    """Show the sign-in page for a review link.

    The link only shows the page. Signing in posts back from it, so a
    cross-site link can never sign the visitor in by itself.
    """
    token_hash = _hash_token(token)

    async with get_admin_session() as db:
        stmt = select(ReviewToken).where(ReviewToken.token_hash == token_hash)
        result = await db.execute(stmt)
        review_token = result.scalar_one_or_none()

        if review_token is None:
            logger.warning("Review token page refused: unknown token")
            return RedirectResponse(url="/login?error=token_invalid", status_code=303)

        if not review_token.is_active:
            logger.warning("Review token page refused: token revoked (id=%s)", review_token.id)
            return RedirectResponse(url="/login?error=token_invalid", status_code=303)

        if review_token.is_expired:
            logger.warning("Review token page refused: token expired (id=%s)", review_token.id)
            return RedirectResponse(url="/login?error=token_invalid", status_code=303)

        label = review_token.label

    return HTMLResponse(_REVIEW_CONFIRMATION_PAGE.substitute(label=html.escape(label)))


async def redeem_review_token(token: str, _redeem: RedeemRequest) -> RedirectResponse:
    """Redeem a review token to create or re-login an anonymous reviewer."""
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

        if review_token.redeemed_at is not None and review_token.redeemed_by_id is None:
            logger.error(
                "Review token is marked redeemed but its reviewer is gone (id=%s)",
                review_token.id,
            )
            return RedirectResponse(url="/login?error=token_invalid", status_code=303)

        if review_token.redeemed_by_id is not None:
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
            await _set_session_cookie(response, str(session.id))
            logger.info(
                "Reviewer re-login via token (token_id=%s, user_id=%s)",
                review_token.id,
                user.id,
            )
            return response

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
        await _set_session_cookie(response, str(session.id))

        logger.info(
            "Reviewer created via token (token_id=%s, user_id=%s, email=%s)",
            review_token.id,
            anon_user.id,
            anon_email,
        )
        return response
