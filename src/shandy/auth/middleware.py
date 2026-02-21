"""
Authentication middleware for SHANDY.

Provides authentication decorators and user context management.
Supports both cookie-based sessions (OAuth) and legacy auth.
"""

import inspect
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

from nicegui import app, ui
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shandy.database.models import Session, User
from shandy.database.session import get_session

logger = logging.getLogger(__name__)


async def get_current_user(db: AsyncSession, session_token: str) -> Optional[User]:
    """
    Get user from session token.

    Eagerly loads the administrator relationship to avoid extra queries.

    Args:
        db: Database session
        session_token: Session token from cookie or storage

    Returns:
        User object if session is valid, None otherwise
    """
    try:
        stmt = (
            select(User)
            .join(Session)
            .where(
                Session.id == session_token,
                Session.expires_at > datetime.now(timezone.utc),
            )
            .options(selectinload(User.administrator))
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    except Exception as e:
        logger.error("Error getting current user: %s", e)
        return None


async def validate_session(session_token: str) -> Optional[dict]:
    """
    Validate a session token and return user info.

    Args:
        session_token: Session token to validate

    Returns:
        Dictionary with user_id, email, name, and is_admin if valid, None otherwise
    """
    try:
        async with get_session() as db:
            user = await get_current_user(db, session_token)
            if user:
                return {
                    "user_id": str(user.id),
                    "email": user.email,
                    "name": user.name,
                    "is_admin": user.administrator is not None,
                }
    except Exception as e:
        logger.error("Session validation error: %s", e)

    return None


def _get_session_token() -> Optional[str]:
    """
    Get session token from cookie or app storage.

    Checks cookies first (for OAuth), then falls back to app.storage.user
    (for legacy auth or dev mode).

    Returns:
        Session token if found, None otherwise
    """
    # Try to get from cookie (set by OAuth flow)
    try:
        if hasattr(ui.context, "client") and hasattr(ui.context.client, "request"):
            session_token = ui.context.client.request.cookies.get("session_token")
            if session_token:
                return session_token
    except Exception:
        logger.debug("Could not read session cookie", exc_info=True)

    # Fall back to app.storage.user (legacy auth or already validated)
    return app.storage.user.get("session_token")


def require_auth(func: Callable) -> Callable:
    """
    Decorator to require authentication for a page.

    Checks for valid session token (from cookie or storage) and redirects to
    login if not authenticated. For async functions, validates session against
    database.

    Args:
        func: Page function to protect

    Returns:
        Wrapped function that checks authentication
    """

    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        # Get session token
        session_token = _get_session_token()
        if not session_token:
            logger.debug("No session token, redirecting to login")
            try:
                app.storage.user["return_to"] = str(ui.context.client.page.path)
            except Exception:
                logger.debug("Could not save return_to path", exc_info=True)
            ui.navigate.to("/login")
            return

        # Validate session against database
        user_info = await validate_session(session_token)
        if not user_info:
            logger.debug("Invalid session token, redirecting to login")
            app.storage.user.clear()
            try:
                app.storage.user["return_to"] = str(ui.context.client.page.path)
            except Exception:
                logger.debug("Could not save return_to path", exc_info=True)
            ui.navigate.to("/login")
            return

        # Store user info in app.storage.user for easy access
        app.storage.user["session_token"] = session_token
        app.storage.user["user_id"] = user_info["user_id"]
        app.storage.user["email"] = user_info["email"]
        app.storage.user["name"] = user_info["name"]
        app.storage.user["is_admin"] = user_info.get("is_admin", False)
        app.storage.user["authenticated"] = True

        # Call the decorated function
        return await func(*args, **kwargs)

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        # Check for session token in cookie (the authoritative source)
        # Don't trust app.storage.user as it persists in browser localStorage
        session_token_from_cookie = None
        try:
            if hasattr(ui.context, "client") and hasattr(ui.context.client, "request"):
                session_token_from_cookie = ui.context.client.request.cookies.get("session_token")
        except Exception:
            logger.debug("Could not read session cookie in sync path", exc_info=True)

        if not session_token_from_cookie:
            # No cookie = not authenticated, clear any stale storage
            logger.debug("No session cookie, redirecting to login")
            try:
                app.storage.user.clear()
                app.storage.user["return_to"] = str(ui.context.client.page.path)
            except (AssertionError, AttributeError):
                # Storage may not be initialized in test environments
                pass
            ui.navigate.to("/login")
            return

        # Cookie exists - check if storage is set up (may need to be validated by async)
        if not app.storage.user.get("authenticated", False):
            # Storage not set up yet - this will be handled by first async page visit
            # For now, trust the cookie and allow access
            # The session will be validated on next async page visit
            pass

        return func(*args, **kwargs)

    # Return appropriate wrapper based on function type
    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


def get_current_user_id() -> Optional[str]:
    """
    Get the current authenticated user's ID.

    Returns:
        User ID if authenticated, None otherwise
    """
    return app.storage.user.get("user_id")


def is_current_user_admin() -> bool:
    """
    Check if the current authenticated user is an administrator.

    Returns:
        True if user is an admin, False otherwise
    """
    return bool(app.storage.user.get("is_admin", False))


def require_admin(func: Callable) -> Callable:
    """
    Decorator to require admin privileges for a page.

    Must be used AFTER @require_auth to ensure user is authenticated first.
    Redirects non-admin users to the homepage with a notification.

    Args:
        func: Page function to protect

    Returns:
        Wrapped function that checks admin status
    """

    @wraps(func)
    async def async_wrapper(*args, **kwargs):
        if not app.storage.user.get("is_admin", False):
            logger.warning(
                "Non-admin user %s attempted to access admin page",
                app.storage.user.get("email", "unknown"),
            )
            ui.notify("Admin access required", type="warning")
            ui.navigate.to("/")
            return
        return await func(*args, **kwargs)

    @wraps(func)
    def sync_wrapper(*args, **kwargs):
        if not app.storage.user.get("is_admin", False):
            logger.warning(
                "Non-admin user %s attempted to access admin page",
                app.storage.user.get("email", "unknown"),
            )
            ui.notify("Admin access required", type="warning")
            ui.navigate.to("/")
            return
        return func(*args, **kwargs)

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper
