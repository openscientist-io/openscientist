"""Live rechecks of the caller's permissions at action time.

Page-level authorization runs once while a page is built. The handlers of a
page that stays open re-read the database at action time through these
helpers, so a logged-out, demoted or deleted caller loses the page's powers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from nicegui import app, ui

from openscientist.auth.middleware import validate_session
from openscientist.job.repository import get_job, get_share_permission

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobAccess:
    """The caller's current permissions on one job, read from the database."""

    is_owner: bool
    can_edit: bool


def _page_session_token() -> str | None:
    """The session token the page validated at build time, or None outside a client."""
    try:
        return app.storage.user.get("session_token")
    except (RuntimeError, AssertionError, AttributeError):
        return None


async def revalidate_admin_session() -> bool:
    """Whether the caller's session still exists and still belongs to an admin."""
    token = _page_session_token()
    if token is None:
        return False
    user_info = await validate_session(token)
    return user_info is not None and bool(user_info.get("is_admin"))


def refuse_stale_admin_session() -> None:
    """Refuse an admin action with a notification, changing nothing."""
    logger.warning("Admin action refused for a session without current admin rights")
    ui.notify("Your admin session is no longer valid. Please refresh the page.", type="warning")


async def current_job_access(job_id: str, expected_user_id: str) -> JobAccess | None:
    """The caller's current access to a job, or None once they can no longer view it.

    The caller's session must still be valid, still belong to
    ``expected_user_id`` (the caller the page was built for), and the job must
    still be visible to that caller under row-level security.
    """
    token = _page_session_token()
    if token is None:
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
