"""
Row-Level Security (RLS) utilities for SHANDY.

Provides middleware and utilities for PostgreSQL Row-Level Security enforcement.
RLS policies control access to database rows based on the current user context.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def set_current_user(session: AsyncSession, user_id: UUID | None) -> None:
    """
    Set the current user ID in the PostgreSQL session for RLS policies.

    This function sets the `app.current_user_id` session variable that RLS
    policies use to determine row visibility and access control.

    The setting is connection-local and persists across transactions within
    the same connection/session.

    Args:
        session: SQLAlchemy async session
        user_id: UUID of the current user, or None to clear

    Example:
        >>> async with get_session() as session:
        ...     await set_current_user(session, user.id)
        ...     # All queries in this session now respect RLS for this user
        ...     jobs = await session.execute(select(Job))
    """
    if user_id is None:
        # Clear the session variable (connection-local, persists across transactions)
        await session.execute(text("SELECT set_config('app.current_user_id', NULL, false)"))
    else:
        # Set the session variable to the user ID (connection-local)
        await session.execute(
            text("SELECT set_config('app.current_user_id', :user_id, false)"),
            {"user_id": str(user_id)},
        )


async def get_current_user(session: AsyncSession) -> UUID | None:
    """
    Get the current user ID from the PostgreSQL session.

    Args:
        session: SQLAlchemy async session

    Returns:
        UUID of the current user, or None if not set

    Example:
        >>> async with get_session() as session:
        ...     user_id = await get_current_user(session)
        ...     print(f"Current user: {user_id}")
    """
    result = await session.execute(text("SELECT current_setting('app.current_user_id', true)"))
    user_id_str = result.scalar_one()

    if user_id_str is None or user_id_str == "":
        return None

    return UUID(user_id_str)


@asynccontextmanager
async def session_with_user(
    session: AsyncSession,
    user_id: UUID | None,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager that sets user context for RLS and cleans up automatically.

    This is a convenience wrapper that combines session management with user
    context setting. It ensures the user ID is properly set before queries
    and cleared afterward.

    Args:
        session: SQLAlchemy async session
        user_id: UUID of the current user, or None for no RLS context

    Yields:
        The same session with user context applied

    Example:
        >>> async with get_session() as session:
        ...     async with session_with_user(session, user.id) as user_session:
        ...         jobs = await user_session.execute(select(Job))
        ...         # Only sees jobs visible to user
    """
    # Store the original user ID
    original_user_id = await get_current_user(session)

    try:
        # Set the new user context
        await set_current_user(session, user_id)
        yield session
    finally:
        # Restore the original user context
        await set_current_user(session, original_user_id)


async def verify_rls_enabled(session: AsyncSession, table_name: str) -> bool:
    """
    Check if RLS is enabled on a specific table.

    Useful for debugging and verification during development and testing.

    Args:
        session: SQLAlchemy async session
        table_name: Name of the table to check

    Returns:
        True if RLS is enabled on the table, False otherwise

    Example:
        >>> async with get_session() as session:
        ...     is_protected = await verify_rls_enabled(session, "jobs")
        ...     print(f"Jobs table RLS: {is_protected}")
    """
    result = await session.execute(
        text(
            """
            SELECT relrowsecurity
            FROM pg_class
            WHERE relname = :table_name
        """
        ),
        {"table_name": table_name},
    )
    row = result.first()
    return row is not None and row[0] is True


async def list_rls_policies(session: AsyncSession, table_name: str) -> list[dict]:
    """
    List all RLS policies defined on a table.

    Returns detailed information about each policy including name, command,
    and definition. Useful for debugging and documentation.

    Args:
        session: SQLAlchemy async session
        table_name: Name of the table to check

    Returns:
        List of dictionaries containing policy information

    Example:
        >>> async with get_session() as session:
        ...     policies = await list_rls_policies(session, "jobs")
        ...     for policy in policies:
        ...         print(f"{policy['name']}: {policy['cmd']}")
    """
    result = await session.execute(
        text(
            """
            SELECT
                polname as name,
                polcmd as cmd,
                pg_get_expr(polqual, polrelid) as using_expr,
                pg_get_expr(polwithcheck, polrelid) as check_expr
            FROM pg_policy
            JOIN pg_class ON pg_class.oid = pg_policy.polrelid
            WHERE pg_class.relname = :table_name
            ORDER BY polname
        """
        ),
        {"table_name": table_name},
    )

    policies = []
    for row in result:
        policies.append(
            {
                "name": row[0],
                "cmd": row[1],  # '*' (ALL), 'r' (SELECT), 'w' (UPDATE), 'a' (INSERT), 'd' (DELETE)
                "using": row[2],
                "check": row[3],
            }
        )

    return policies
