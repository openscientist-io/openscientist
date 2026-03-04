"""
Database session management for Open Scientist.

Provides async session factories and dependency injection for FastAPI/NiceGUI.

Session Types:
    - get_session(): Standard session with RLS enforced via SET ROLE open_scientist_app
    - get_admin_session(): Admin session that bypasses RLS via elevated role
    - AsyncSessionLocal(): Low-level session factory (also enforces RLS when used
      without thread_safe, or via SET ROLE in thread-safe mode)

For the dual-engine pattern, get_session() connects as the main database user
then immediately does SET ROLE open_scientist_app to drop privileges. The open_scientist_app
role is a non-superuser NOLOGIN role that is subject to RLS policies.
"""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from open_scientist.settings import get_settings

from .engine import get_admin_engine, get_engine

logger = logging.getLogger(__name__)

# Lazy session factories (initialized on first use)
_session_factory: async_sessionmaker[AsyncSession] | None = None
_admin_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory for standard operations."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return _session_factory


def _get_admin_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory for admin operations.

    This factory uses the admin engine which connects with an elevated
    PostgreSQL role that bypasses RLS policies.
    """
    global _admin_session_factory
    if _admin_session_factory is None:
        _admin_session_factory = async_sessionmaker(
            get_admin_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return _admin_session_factory


def _create_fresh_session_factory() -> async_sessionmaker[AsyncSession]:
    """Create a fresh session factory for the current event loop.

    This creates a new engine with NullPool (no connection pooling)
    for use in worker threads or contexts with different event loops.
    The engine and factory are NOT cached because asyncpg connections
    are bound to the event loop they were created in, and asyncio.run()
    creates a new event loop each time.
    """
    settings = get_settings()
    database_url = settings.database.effective_database_url

    # Create a fresh engine with NullPool - no caching since each
    # asyncio.run() creates a new event loop and connections are loop-bound
    thread_engine = create_async_engine(
        database_url,
        poolclass=NullPool,  # No pooling - each connection is fresh
        echo=settings.database.sql_echo,
    )
    return async_sessionmaker(
        thread_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


def async_session_local(thread_safe: bool = False) -> AsyncSession:
    """Create a new async session.

    Args:
        thread_safe: If True, creates a fresh session factory with NullPool,
                     safe for use in worker threads or separate event loops.
                     Each call creates a new engine to avoid event loop conflicts.
                     Default False uses the shared engine (main event loop only).

    Returns:
        A new AsyncSession instance.

    Note:
        The returned session does NOT automatically SET ROLE open_scientist_app.
        Callers must use the session as a context manager and call
        set_current_user() to set RLS context, or use get_session() instead.
    """
    if thread_safe:
        return _create_fresh_session_factory()()
    return _get_session_factory()()


# Backward-compatible alias for older imports.
AsyncSessionLocal = async_session_local


async def _set_app_role(session: AsyncSession) -> None:
    """Drop privileges to open_scientist_app role for RLS enforcement.

    The main database user (open_scientist) is typically a superuser that bypasses
    all RLS policies. By switching to open_scientist_app (a non-superuser NOLOGIN role),
    the session becomes subject to RLS policies.

    This must be called on every new session before user-facing queries.
    If the open_scientist_app role does not exist, this will raise — that's intentional.
    A silent fallback to superuser would bypass all RLS.
    """
    await session.execute(text("SET ROLE open_scientist_app"))


async def _clear_rls_user_context(session: AsyncSession) -> None:
    """Clear app.current_user_id to prevent context leakage across pooled connections."""
    with suppress(Exception):
        await session.execute(text("SELECT set_config('app.current_user_id', NULL, false)"))


async def _reset_role(session: AsyncSession) -> None:
    """Restore the original database role after session use."""
    with suppress(Exception):
        await session.execute(text("RESET ROLE"))


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency to provide a database session with RLS enforced.

    Connects as the main database user, then drops privileges to open_scientist_app
    via SET ROLE. The open_scientist_app role is subject to RLS policies, ensuring
    that queries only return rows the current user is authorized to see.

    Callers must still call set_current_user(session, user_id) to set the
    RLS context for the specific user.

    Usage in FastAPI (as dependency):
        @app.get("/items")
        async def get_items(session: AsyncSession = Depends(get_session)):
            ...

    Usage in NiceGUI or standalone (as context manager):
        async with get_session_ctx() as session:
            await set_current_user(session, user.id)
            ...

    Yields:
        AsyncSession: A SQLAlchemy async session with RLS enforced.
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            # Drop to open_scientist_app role so RLS policies are enforced
            await _set_app_role(session)
            await _clear_rls_user_context(session)
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await _clear_rls_user_context(session)
            await _reset_role(session)
            await session.close()


# Context manager wrapper for get_session (for NiceGUI/standalone use).
# FastAPI Depends uses get_session directly (plain async generator).
get_session_ctx = asynccontextmanager(get_session)


@asynccontextmanager
async def get_admin_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency to provide a database session with admin privileges.

    Sessions from this factory bypass RLS policies by connecting with
    the open_scientist_admin PostgreSQL role which has BYPASSRLS privilege.

    The role is created by docker/postgres/init.sql in production or by
    the test setup in tests/conftest.py.

    Use this only for:
    - Background schedulers (no user context available)
    - Admin-authenticated endpoints (verified by @require_admin)
    - Migrations and schema changes
    - Test fixtures that need to create data across tenants

    WARNING: This grants full database access. Never expose to user-facing APIs.

    Usage:
        async with get_admin_session() as session:
            # RLS bypassed via BYPASSRLS role privilege
            all_jobs = await session.execute(select(Job))
            ...

    Yields:
        AsyncSession: A SQLAlchemy async session with admin privileges.
    """
    # Use the admin session factory which connects with elevated privileges
    factory = _get_admin_session_factory()
    async with factory() as session:
        try:
            await _clear_rls_user_context(session)
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await _clear_rls_user_context(session)
            await session.close()
