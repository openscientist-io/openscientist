"""Row-Level Security (RLS) utilities for OpenScientist."""

from collections.abc import Collection
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

#: The role user sessions run as. The policies on user data are written for it.
APP_ROLE = "openscientist_app"
_ROLE_KEY = "openscientist.rls_app_role"
_USER_KEY = "openscientist.rls_user_id"


@event.listens_for(Session, "after_begin")
def _apply_rls_to_transaction(
    session: Session, _transaction: SessionTransaction, connection: Connection
) -> None:
    """Give every transaction the session's role and user. Both are transaction-local,
    so a commit cannot shed them and a pooled connection never keeps them."""
    if session.info.get(_ROLE_KEY):
        connection.exec_driver_sql(f"SET LOCAL ROLE {APP_ROLE}")
    if _USER_KEY in session.info:
        connection.execute(
            text("SELECT set_config('app.current_user_id', :user_id, true)"),
            {"user_id": session.info[_USER_KEY]},
        )


async def use_app_role(session: AsyncSession) -> None:
    """Run every transaction of ``session`` as ``openscientist_app``, the open one included."""
    session.info[_ROLE_KEY] = True
    if session.in_transaction():
        await session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))


async def set_current_user(session: AsyncSession, user_id: UUID | None) -> None:
    """Set the user that RLS policies see as ``app.current_user_id``.

    The user applies to every transaction of the session, the open one included,
    and to no other session that later gets the same pooled connection.
    """
    value = None if user_id is None else str(user_id)
    session.info[_USER_KEY] = value
    if session.in_transaction():
        await session.execute(
            text("SELECT set_config('app.current_user_id', :user_id, true)"),
            {"user_id": value},
        )


async def tables_without_enforced_rls(conn: AsyncConnection, tables: Collection[str]) -> list[str]:
    """The tables among ``tables`` that are missing from the public schema, or
    whose RLS is not both enabled and forced, sorted by name."""
    result = await conn.execute(
        text(
            "SELECT relname FROM pg_class"
            " WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
            " AND relrowsecurity AND relforcerowsecurity AND relname = ANY(:tables)"
        ),
        {"tables": list(tables)},
    )
    enforced = set(result.scalars())
    return sorted(set(tables) - enforced)
