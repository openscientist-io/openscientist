"""
Database session management for SHANDY.

Provides async session factories and dependency injection for FastAPI/NiceGUI.

Session Types:
    - get_session(): Standard session with RLS enforced by PostgreSQL role
    - get_admin_session(): Admin session that bypasses RLS via elevated role

For the dual-engine pattern, get_admin_session() uses a separate connection pool
with an elevated PostgreSQL role. This is the recommended approach for production
as it enforces RLS at the database level, not just application code.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from shandy.settings import get_settings

from .engine import get_admin_engine, get_engine

# Lazy session factories (initialized on first use)
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_admin_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


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


def AsyncSessionLocal(thread_safe: bool = False) -> AsyncSession:  # noqa: N802
    """Create a new async session.

    Args:
        thread_safe: If True, creates a fresh session factory with NullPool,
                     safe for use in worker threads or separate event loops.
                     Each call creates a new engine to avoid event loop conflicts.
                     Default False uses the shared engine (main event loop only).

    Returns:
        A new AsyncSession instance.
    """
    if thread_safe:
        return _create_fresh_session_factory()()
    return _get_session_factory()()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency to provide a database session with standard privileges.

    Sessions from this factory are subject to RLS policies. Use this for
    all user-facing operations where the user context is available.

    Usage in FastAPI:
        @app.get("/items")
        async def get_items(session: AsyncSession = Depends(get_session)):
            ...

    Usage in NiceGUI or standalone:
        async with get_session() as session:
            await set_current_user(session, user.id)
            ...

    Yields:
        AsyncSession: A SQLAlchemy async session.
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_admin_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency to provide a database session with admin privileges.

    Sessions from this factory bypass RLS policies by connecting with
    the shandy_admin PostgreSQL role which has BYPASSRLS privilege.

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
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
