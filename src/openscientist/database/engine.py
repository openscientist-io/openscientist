"""Database engine configuration for OpenScientist.

An asyncpg connection belongs to the event loop that opened it, so each loop
gets its own pooled engines, one per database login, created on first use.
"""

import asyncio
import threading
import weakref
from collections.abc import AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from openscientist.database.retry import guard_connect
from openscientist.settings import get_settings

load_dotenv()


class _LoopEngines:
    """One loop's engines by URL, and the async generator that disposes them
    when the loop shuts its async generators down."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.by_url: dict[str, AsyncEngine] = {}
        self.keeper = _dispose_at_shutdown(loop, self.by_url)
        # The keeper awaits nothing before its yield, so one step runs it there
        # without the loop, and that first step registers it with this loop.
        try:
            self.keeper.asend(None).send(None)
        except StopIteration:
            return
        raise AssertionError("the engine keeper awaited before reaching its yield")


_loop_engines: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopEngines] = (
    weakref.WeakKeyDictionary()
)
_loop_engines_lock = threading.Lock()


async def _dispose_at_shutdown(
    loop: asyncio.AbstractEventLoop, by_url: dict[str, AsyncEngine]
) -> AsyncGenerator[None, None]:
    try:
        yield
    finally:
        with _loop_engines_lock:
            _loop_engines.pop(loop, None)
        for engine in by_url.values():
            await engine.dispose()


def _engine_for(database_url: str) -> AsyncEngine:
    """The running loop's pooled engine for ``database_url``."""
    loop = asyncio.get_running_loop()
    with _loop_engines_lock:
        engines = _loop_engines.get(loop)
        if engines is None:
            engines = _loop_engines[loop] = _LoopEngines(loop)
        engine = engines.by_url.get(database_url)
        if engine is None:
            settings = get_settings()
            engine = engines.by_url[database_url] = guard_connect(
                create_async_engine(
                    database_url,
                    pool_size=20,
                    max_overflow=10,
                    pool_timeout=30,
                    pool_recycle=3600,
                    pool_pre_ping=True,
                    echo=settings.database.sql_echo,
                    # Statements bind session tokens and other secrets, and
                    # database errors and the echo log reach the logs.
                    hide_parameters=True,
                ),
                retry_for=settings.database.connect_retry_seconds,
            )
    return engine


def get_engine() -> AsyncEngine:
    """The running loop's engine for standard operations.

    It connects with ``DATABASE_URL``, whose login works under RLS policies.
    """
    return _engine_for(get_settings().database.effective_database_url)


def get_admin_engine() -> AsyncEngine:
    """The running loop's engine for privileged operations.

    It connects with ``ADMIN_DATABASE_URL``, whose role bypasses RLS policies
    (table owner or BYPASSRLS grant). Use it only for background schedulers,
    admin endpoints, migrations and test fixtures.
    """
    return _engine_for(get_settings().database.required_admin_database_url())
