"""
Database session management for SHANDY.

Provides async session factory and dependency injection for FastAPI/NiceGUI.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .engine import get_engine

# Lazy session factory (initialized on first use)
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


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


# Keep for backward compatibility but make it a function call
def AsyncSessionLocal() -> AsyncSession:  # noqa: N802
    """Create a new async session. Lazy-initializes the engine on first call."""
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
