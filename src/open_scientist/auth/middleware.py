"""
Authentication middleware for SHANDY.

Provides authentication decorators and user context management.
Supports cookie-backed session authentication.
"""

import inspect
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from nicegui import app, ui
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shandy.async_tasks import run_sync
from shandy.database.models import Session, User
from shandy.database.session import get_admin_session

logger = logging.getLogger(__name__)


async def get_current_user(db: AsyncSession, session_token: str) -> User | None:
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
                Session.expires_at > datetime.now(UTC),
            )
            .options(selectinload(User.administrator))
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    except Exception as e:
        logger.error("Error getting current user: %s", e)
        return None


async def validate_session(session_token: str) -> dict[str, Any] | None:
    """
    Validate a session token and return user info.

    Args:
        session_token: Session token to validate

    Returns:
        Dictionary with user_id, email, name, admin/approval flags if valid, None otherwise
    """
    try:
        async with get_admin_session() as db:
            user = await get_current_user(db, session_token)
            if user:
                is_admin = user.administrator is not None
                can_start_jobs = bool(user.is_approved or is_admin)
                return {
                    "user_id": str(user.id),
                    "email": user.email,
                    "name": user.name,
                    "is_admin": is_admin,
                    "is_approved": bool(user.is_approved),
                    "can_start_jobs": can_start_jobs,
                }
    except Exception as e:
        logger.error("Session validation error: %s", e)

    return None


def _get_session_token() -> str | None:
    """
    Get session token from cookie or app storage.

    Checks cookies first, then falls back to NiceGUI user storage.

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

    # Fall back to app.storage.user for authenticated UI flows.
    return app.storage.user.get("session_token")


def _store_authenticated_user(session_token: str, user_info: dict[str, Any]) -> None:
    """Store authenticated user info in NiceGUI storage."""
    app.storage.user["session_token"] = session_token
    app.storage.user["user_id"] = user_info["user_id"]
    app.storage.user["email"] = user_info["email"]
    app.storage.user["name"] = user_info["name"]
    app.storage.user["is_admin"] = user_info.get("is_admin", False)
    app.storage.user["is_approved"] = user_info.get("is_approved", False)
    app.storage.user["can_start_jobs"] = user_info.get("can_start_jobs", False)
    app.storage.user["authenticated"] = True


def _save_return_to_path() -> None:
    """Save the current path to return to after login."""
    try:
        app.storage.user["return_to"] = str(ui.context.client.page.path)
    except Exception:
        logger.debug("Could not save return_to path", exc_info=True)


def _clear_user_storage(tolerate_uninitialized: bool = False) -> None:
    """Clear NiceGUI user storage with optional tolerance for test contexts."""
    if tolerate_uninitialized:
        with suppress(AssertionError, AttributeError):
            app.storage.user.clear()
        return
    app.storage.user.clear()


def _redirect_to_login(
    *,
    clear_storage: bool = False,
    tolerate_uninitialized_storage: bool = False,
) -> None:
    """Persist return path and navigate to login."""
    if clear_storage:
        _clear_user_storage(tolerate_uninitialized=tolerate_uninitialized_storage)
    _save_return_to_path()
    ui.navigate.to("/login")


def require_auth(func: Callable[..., Any]) -> Callable[..., Any]:
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
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        # Get session token
        session_token = _get_session_token()
        if not session_token:
            logger.debug("No session token, redirecting to login")
            _redirect_to_login()
            return None

        # Validate session against database
        user_info = await validate_session(session_token)
        if not user_info:
            logger.debug("Invalid session token, redirecting to login")
            _redirect_to_login(clear_storage=True)
            return None

        # Store user info in app.storage.user for easy access
        _store_authenticated_user(session_token, user_info)

        # Call the decorated function
        return await func(*args, **kwargs)

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        # Strictly validate every protected sync page against the database.
        session_token = _get_session_token()
        if not session_token:
            logger.debug("No session cookie, redirecting to login")
            _redirect_to_login(clear_storage=True, tolerate_uninitialized_storage=True)
            return None

        try:
            user_info = run_sync(validate_session(session_token))
        except Exception:
            logger.error("Session validation failed in sync auth path", exc_info=True)
            user_info = None

        if not user_info:
            logger.debug("Invalid session token in sync path, redirecting to login")
            _redirect_to_login(clear_storage=True, tolerate_uninitialized_storage=True)
            return None

        _store_authenticated_user(session_token, user_info)

        return func(*args, **kwargs)

    # Return appropriate wrapper based on function type
    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper


def get_current_user_id() -> str | None:
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


def is_current_user_approved() -> bool:
    """
    Check if the current authenticated user is approved to start jobs.

    Returns:
        True if approved, False otherwise
    """
    return bool(app.storage.user.get("is_approved", False))


def can_current_user_start_jobs() -> bool:
    """
    Check if the current user can start jobs.

    Administrators are allowed even if their explicit approval flag is false.

    Returns:
        True if user can start jobs, False otherwise
    """
    if "can_start_jobs" in app.storage.user:
        return bool(app.storage.user.get("can_start_jobs"))
    return bool(
        app.storage.user.get("is_approved", False) or app.storage.user.get("is_admin", False)
    )


def require_admin(func: Callable[..., Any]) -> Callable[..., Any]:
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
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        if not app.storage.user.get("is_admin", False):
            logger.warning(
                "Non-admin user %s attempted to access admin page",
                app.storage.user.get("email", "unknown"),
            )
            ui.notify("Admin access required", type="warning")
            ui.navigate.to("/")
            return None
        return await func(*args, **kwargs)

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if not app.storage.user.get("is_admin", False):
            logger.warning(
                "Non-admin user %s attempted to access admin page",
                app.storage.user.get("email", "unknown"),
            )
            ui.notify("Admin access required", type="warning")
            ui.navigate.to("/")
            return None
        return func(*args, **kwargs)

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper
