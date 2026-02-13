"""
Database session management for SHANDY.

Provides async session factory and dependency injection for FastAPI/NiceGUI.
"""

import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from shandy.settings import get_settings

from .engine import get_engine

# Lazy session factory (initialized on first use)
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None

# Thread-local storage for per-thread engines (used when running in worker threads)
_thread_local = threading.local()


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the async session factory."""
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


def _get_thread_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create a session factory for the current thread.

    This creates a separate engine with NullPool (no connection pooling)
    for use in worker threads, avoiding event loop conflicts with asyncpg.
    """
    if not hasattr(_thread_local, "session_factory"):
        settings = get_settings()
        database_url = settings.database.effective_database_url

        # Create a thread-local engine with NullPool to avoid event loop issues
        thread_engine = create_async_engine(
            database_url,
            poolclass=NullPool,  # No pooling - each connection is fresh
            echo=settings.database.sql_echo,
        )
        _thread_local.session_factory = async_sessionmaker(
            thread_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )
    return cast(async_sessionmaker[AsyncSession], _thread_local.session_factory)


def AsyncSessionLocal(thread_safe: bool = False) -> AsyncSession:  # noqa: N802
    """Create a new async session.

    Args:
        thread_safe: If True, creates a session using a thread-local engine
                     with NullPool, safe for use in worker threads with their
                     own event loops. Default False uses the shared engine.

    Returns:
        A new AsyncSession instance.
    """
    if thread_safe:
        return _get_thread_session_factory()()
    return _get_session_factory()()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency to provide a database session.

    Usage in FastAPI:
        @app.get("/items")
        async def get_items(session: AsyncSession = Depends(get_session)):
            ...

    Usage in NiceGUI or standalone:
        async with get_session() as session:
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
