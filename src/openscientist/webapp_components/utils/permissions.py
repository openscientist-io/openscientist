"""Live rechecks of the caller's permissions at action time.

Page-level authorization runs once while a page is built. The handlers of a
page that stays open re-read the database at action time through these
helpers, so a logged-out, demoted or deleted caller loses the page's powers.
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from nicegui import ui
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth.middleware import get_session_token, validate_session
from openscientist.database.session import get_admin_session
from openscientist.job.repository import get_job, get_share_permission

logger = logging.getLogger(__name__)


class AdminAccess:
    """Proof that the caller was an admin with a live session when it was
    issued. Admin pages open the row-level-security-bypassing database session
    only through it, so no admin read or write skips the recheck.

    Only ``admin_access`` builds one, and a handler holds it for one action.
    """

    def session(self) -> AbstractAsyncContextManager[AsyncSession]:
        """The admin database session."""
        return get_admin_session()


async def admin_access() -> AdminAccess | None:
    """An ``AdminAccess`` while the caller's session still exists and still
    belongs to an admin. Otherwise the caller is told to refresh the page and
    gets None."""
    if await revalidate_admin_session():
        return AdminAccess()
    _refuse_stale_admin()
    return None


async def admin_user_id() -> UUID | None:
    """The caller's user id while their session still exists and still belongs
    to an admin. Otherwise the caller is told to refresh the page and gets None."""
    user_info = await revalidate_session()
    if user_info is not None and user_info["is_admin"]:
        return UUID(user_info["user_id"])
    _refuse_stale_admin()
    return None


def _refuse_stale_admin() -> None:
    logger.warning("Admin action refused for a session without current admin rights")
    ui.notify("Your admin session is no longer valid. Please refresh the page.", type="warning")


@dataclass(frozen=True)
class JobAccess:
    """The caller's current permissions on one job, read from the database."""

    is_owner: bool
    can_edit: bool


async def revalidate_admin_session() -> bool:
    """Whether the caller's session still exists and still belongs to an admin."""
    token = get_session_token()
    if not token:
        return False
    user_info = await validate_session(token)
    return user_info is not None and bool(user_info.get("is_admin"))


async def revalidate_session() -> dict[str, Any] | None:
    """The caller's current user info, re-read from the login cookie and the
    database, or None once the session is gone or the user is deleted.

    Page handlers call it at action time, so a caller whose session was
    revoked or expired while the page stayed open loses the page's powers.
    The caller checks the returned flags, such as ``can_start_jobs``, for the
    current state of the user.
    """
    token = get_session_token()
    if not token:
        return None
    return await validate_session(token)


async def current_job_access(job_id: str, expected_user_id: str) -> JobAccess | None:
    """The caller's current access to a job, or None once they can no longer view it.

    The caller's session must still be valid, still belong to
    ``expected_user_id`` (the caller the page was built for), and the job must
    still be visible to that caller under row-level security.
    """
    token = get_session_token()
    if not token:
        return None
    user_info = await validate_session(token)
    if user_info is None or user_info.get("user_id") != expected_user_id:
        return None
    caller = UUID(expected_user_id)
    db_job = await get_job(job_id, user_id=caller)
    if db_job is None:
        return None
    if db_job.owner_id == caller:
        return JobAccess(is_owner=True, can_edit=True)
    permission = await get_share_permission(job_id, caller)
    return JobAccess(is_owner=False, can_edit=permission == "edit")
