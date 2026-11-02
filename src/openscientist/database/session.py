"""Database session management for OpenScientist.

Every session binds the pooled engine of the loop it is opened on, so it works
in any thread with a running loop.
"""

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .engine import get_admin_engine, get_engine
from .rls import set_current_user, use_app_role

logger = logging.getLogger(__name__)


def _session(engine: AsyncEngine) -> AsyncSession:
    return AsyncSession(engine, expire_on_commit=False, autoflush=False)


def thread_safe_admin_session() -> AsyncSession:
    """An admin session for system work in the web process that acts for no user.

    Connects with ``ADMIN_DATABASE_URL`` (``openscientist_admin``, BYPASSRLS).
    """
    return _session(get_admin_engine())


@asynccontextmanager
async def thread_safe_user_session(user_id: UUID) -> AsyncIterator[AsyncSession]:
    """A session whose every transaction runs as ``user_id`` under RLS."""
    async with _session(get_engine()) as session:
        await use_app_role(session)
        await set_current_user(session, user_id)
        yield session


#: Opens the caller's session inside the coroutine that uses it.
SessionOpener = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def async_session_local() -> AsyncSession:
    """Create a new async session.

    The returned session runs as the login role. Callers that act for a user
    call use_app_role() and set_current_user(), or use get_session() instead.
    """
    return _session(get_engine())


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency to provide a database session with RLS enforced.

    Connects as the main database user, and runs every transaction as
    openscientist_app, which is subject to RLS policies, so queries only return
    rows the current user is authorized to see. Callers must still call
    set_current_user(session, user_id) to set the RLS context for the specific
    user.
    """
    async with _session(get_engine()) as session:
        try:
            await use_app_role(session)
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Context manager wrapper for get_session (for NiceGUI/standalone use).
# FastAPI Depends uses get_session directly (plain async generator).
get_session_ctx = asynccontextmanager(get_session)


@asynccontextmanager
async def get_admin_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency to provide a database session with admin privileges.

    Sessions from this factory bypass RLS policies by connecting with the
    openscientist_admin role, which has BYPASSRLS. Use it only for background
    schedulers, admin endpoints, migrations and test fixtures. Never expose it
    to user-facing APIs.
    """
    async with _session(get_admin_engine()) as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
