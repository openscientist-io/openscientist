"""Granting and removing admin rights on the acting admin's own session.

Both writes run as the acting user under row-level security, so Postgres
refuses them for a user who is not an admin, whatever the page checked.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Administrator
from openscientist.database.models.administrator import NOT_AN_ADMIN_SQLSTATE
from openscientist.database.session import thread_safe_user_session

_NOT_AN_ADMIN = "Only admins can change admin rights"
_SELF_REMOVAL = "You cannot remove your own admin rights"
_FOREIGN_KEY_VIOLATION = "23503"


def _sqlstate(error: DBAPIError) -> str | None:
    return getattr(error.orig, "sqlstate", None)


async def _is_admin(session: AsyncSession) -> bool:
    return bool(await session.scalar(select(func.current_user_is_admin())))


async def grant_admin(actor_id: UUID, user_id: UUID) -> tuple[bool, str]:
    """Make ``user_id`` an admin, recorded as granted by ``actor_id``.

    Returns:
        Tuple of (success, message)
    """
    async with thread_safe_user_session(actor_id) as session:
        if not await _is_admin(session):
            return False, _NOT_AN_ADMIN
        try:
            granted = await session.scalar(
                insert(Administrator)
                .values(user_id=user_id, granted_by=actor_id)
                .on_conflict_do_nothing(index_elements=[Administrator.user_id])
                .returning(Administrator.user_id)
            )
        except DBAPIError as error:
            if _sqlstate(error) == _FOREIGN_KEY_VIOLATION:
                return False, "User not found"
            if _sqlstate(error) == NOT_AN_ADMIN_SQLSTATE:
                return False, _NOT_AN_ADMIN
            raise
        await session.commit()
    if granted is None:
        return False, "User is already an admin"
    return True, "User is now an admin"


async def revoke_admin(actor_id: UUID, user_id: UUID) -> tuple[bool, str]:
    """Remove the admin rights of ``user_id``, never those of ``actor_id``.

    Returns:
        Tuple of (success, message)
    """
    if actor_id == user_id:
        return False, _SELF_REMOVAL
    async with thread_safe_user_session(actor_id) as session:
        if not await _is_admin(session):
            return False, _NOT_AN_ADMIN
        try:
            revoked = await session.scalar(
                delete(Administrator)
                .where(Administrator.user_id == user_id)
                .returning(Administrator.user_id)
            )
        except DBAPIError as error:
            if _sqlstate(error) == NOT_AN_ADMIN_SQLSTATE:
                return False, _NOT_AN_ADMIN
            raise
        await session.commit()
    if revoked is None:
        return False, "User is not an admin"
    return True, "User is no longer an admin"
