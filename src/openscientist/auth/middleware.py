"""Authentication middleware for OpenScientist."""

import asyncio
import inspect
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any
from urllib.parse import quote, urlsplit

from nicegui import app, ui
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.requests import Request
from starlette.responses import RedirectResponse

from openscientist.database.models import Session, User
from openscientist.database.retry import is_connection_error
from openscientist.database.session import get_admin_session

logger = logging.getLogger(__name__)

#: How far inside a page's ``response_timeout`` the session check's deadline
#: sits, so the outage page is still sent before NiceGUI's own timeout.
_OUTAGE_PAGE_SECONDS = 0.5

#: Where a signed-in visitor lands when no page asked them to sign in.
HOME_PATH = "/jobs"

#: Checks cancelled at their deadline, held until their database driver
#: finishes cleaning up, which on a hung connection outlasts the deadline.
_abandoned_checks: set[asyncio.Task[dict[str, Any] | None]] = set()


async def get_current_user(db: AsyncSession, session_token: str) -> User | None:
    """Get the user for a session token."""
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


async def validate_session(session_token: str) -> dict[str, Any] | None:
    """Validate a session token and return user info.

    Database errors propagate, so a failure never reads as a missing session.
    """
    async with get_admin_session() as db:
        user = await get_current_user(db, session_token)
        if user is None:
            return None
        is_admin = user.administrator is not None
        return {
            "user_id": str(user.id),
            "email": user.email,
            "name": user.name,
            "is_admin": is_admin,
            "is_approved": bool(user.is_approved),
            "can_start_jobs": bool(user.is_approved or is_admin),
        }


def get_session_token() -> str | None:
    """The login cookie's session token on the current page's request, or None outside a page.

    Event handlers run in the page's client, so they read the cookie the page was built with."""
    try:
        if hasattr(ui.context, "client") and hasattr(ui.context.client, "request"):
            return ui.context.client.request.cookies.get("session_token")
    except Exception:
        logger.debug("Could not read session cookie", exc_info=True)
    return None


def _store_authenticated_user(user_info: dict[str, Any]) -> None:
    """Store the validated user's info in NiceGUI storage for event handlers.

    The token stays in the cookie, which every request re-validates, so it is
    never copied into the per-browser storage."""
    app.storage.user["user_id"] = user_info["user_id"]
    app.storage.user["email"] = user_info["email"]
    app.storage.user["name"] = user_info["name"]
    app.storage.user["is_admin"] = user_info.get("is_admin", False)
    app.storage.user["is_approved"] = user_info.get("is_approved", False)
    app.storage.user["can_start_jobs"] = user_info.get("can_start_jobs", False)
    app.storage.user["authenticated"] = True


def redirect_to(path: str) -> RedirectResponse:
    """A 303 to ``path``, which a page returns so the server answers the
    redirect and the browser never paints the page it leaves."""
    return RedirectResponse(url=path, status_code=303)


def _is_local_path(path: object) -> bool:
    """Whether ``path`` stays on this site, for a redirect target read back
    from a request."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return False
    parts = urlsplit(path)
    return not parts.scheme and not parts.netloc and "\\" not in path


def safe_next(path: object) -> str:
    """The page a visitor asked for, or the home page when ``path`` is
    missing or would leave the site."""
    if isinstance(path, str) and _is_local_path(path):
        return path
    return HOME_PATH


def return_to(request: Request) -> str:
    """The page a visitor asked for, carried by the request's ``next``
    parameter, or the home page when there is none or it would leave the site."""
    return safe_next(request.query_params.get("next"))


def _login_url() -> str:
    """The login page's URL, carrying the page the visitor asked for as its
    ``next`` parameter when that page stays on this site."""
    url = ui.context.client.request.url
    target = f"{url.path}?{url.query}" if url.query else url.path
    if target != "/login" and _is_local_path(target):
        return f"/login?next={quote(target)}"
    return "/login"


def _redirect_to_login(*, clear_storage: bool = False) -> RedirectResponse:
    """Send the visitor to the login page, carrying the page they asked for."""
    if clear_storage:
        app.storage.user.clear()
    return redirect_to(_login_url())


def _show_database_unavailable() -> None:
    """Tell the visitor the database is unreachable, leaving their sign-in alone."""
    with ui.column().classes("w-full items-center mt-16 gap-4"):
        ui.label("OpenScientist cannot reach its database right now.").classes("text-h6")
        ui.label("You are still signed in. Try again in a moment.").classes("text-grey-7")
        ui.button("Retry", icon="refresh", on_click=ui.navigate.reload)


def require_auth(func: Callable[..., Any]) -> Callable[..., Any]:
    """Require authentication for an async page.

    A database that accepts connects but never answers raises nothing within
    the page's ``response_timeout``, after which NiceGUI serves its own error
    page, so the session check runs as a task with a deadline just inside it.
    When the deadline expires the check is cancelled and the outage page
    shows, without waiting for the driver to release the hung connection. The
    page build itself runs on, bounded by the page's ``response_timeout`` and
    its own per-query error handling.

    Sync pages are refused. Their session check would block the event loop.
    """
    if not inspect.iscoroutinefunction(func):
        raise TypeError(f"require_auth needs an async page, and {func.__qualname__} is sync")

    @wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        session_token = get_session_token()
        if not session_token:
            logger.debug("No session token, redirecting to login")
            return _redirect_to_login()

        page = ui.context.client.page
        deadline = page.response_timeout - _OUTAGE_PAGE_SECONDS
        check = asyncio.create_task(validate_session(session_token))
        try:
            done, _ = await asyncio.wait({check}, timeout=deadline)
        except asyncio.CancelledError:
            _abandon(check)
            raise
        if not done:
            _abandon(check)
            logger.error(
                "The database did not answer within %.1f s while checking a session for %s",
                deadline,
                page.path,
            )
            ui.context.client.content.clear()
            _show_database_unavailable()
            return None
        try:
            user_info = check.result()
        except Exception as e:
            if not is_connection_error(e):
                raise
            logger.error("Database unreachable while checking a session: %s", e)
            ui.context.client.content.clear()
            _show_database_unavailable()
            return None
        if user_info is None:
            logger.debug("Invalid session token, redirecting to login")
            return _redirect_to_login(clear_storage=True)

        _store_authenticated_user(user_info)
        return await func(*args, **kwargs)

    return async_wrapper


def _abandon(check: asyncio.Task[dict[str, Any] | None]) -> None:
    """Cancel ``check`` and hold it until it ends."""
    check.cancel()
    _abandoned_checks.add(check)
    check.add_done_callback(_forget_abandoned)


def _forget_abandoned(check: asyncio.Task[dict[str, Any] | None]) -> None:
    _abandoned_checks.discard(check)
    if not check.cancelled() and (error := check.exception()) is not None:
        logger.debug("A session check abandoned at its deadline ended with %r", error)


async def session_still_valid_for(user_id: str) -> bool:
    """Whether the page's session is still valid and still that user's.

    Periodic ticks call it before touching data. It re-reads the
    ``session_token`` cookie the page was built with against the sessions
    table, in one primary-key lookup, and is true while the row is
    unexpired and belongs to ``user_id``, the user the page was built for.
    A database it cannot reach cannot prove the session is gone, so it
    reads as still valid and the tick's own data load reports the outage,
    the way a page build does.
    """
    token = get_session_token()
    if token is None:
        return False
    try:
        user_info = await validate_session(token)
    except Exception as e:
        if not is_connection_error(e):
            raise
        logger.debug("Database unreachable while re-checking a session: %s", e)
        return True
    return user_info is not None and user_info.get("user_id") == user_id


def go_to_login() -> None:
    """Send the current client to the login page, carrying the page it
    leaves as ``next`` the way ``require_auth`` builds it."""
    ui.navigate.to(_login_url())


def get_current_user_id() -> str | None:
    """Get the current authenticated user's ID."""
    return app.storage.user.get("user_id")


def is_current_user_admin() -> bool:
    """Check if the current authenticated user is an administrator."""
    return bool(app.storage.user.get("is_admin", False))


def can_current_user_start_jobs() -> bool:
    """Check if the current user can start jobs.

    Administrators are allowed even if their explicit approval flag is false.
    """
    if "can_start_jobs" in app.storage.user:
        return bool(app.storage.user.get("can_start_jobs"))
    return bool(
        app.storage.user.get("is_approved", False) or app.storage.user.get("is_admin", False)
    )


def require_admin(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to require admin privileges for a page.

    Use after ``@require_auth`` so the user is authenticated first. A
    non-admin is sent to the home page by the server.
    """

    def refused() -> RedirectResponse | None:
        if app.storage.user.get("is_admin", False):
            return None
        logger.warning(
            "Non-admin user %s attempted to access admin page",
            app.storage.user.get("email", "unknown"),
        )
        return redirect_to(HOME_PATH)

    @wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        return refused() or await func(*args, **kwargs)

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        return refused() or func(*args, **kwargs)

    if inspect.iscoroutinefunction(func):
        return async_wrapper
    return sync_wrapper
