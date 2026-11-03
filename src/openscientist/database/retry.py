"""Telling a lost database connection from other database errors, and retrying it.

A refused or reset connect leaves the engine as a bare ``OSError``, which reads
the same as a file error, so :func:`guard_connect` marks it where it happens."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.util import await_only

logger = logging.getLogger(__name__)

#: How long the web's background work waits between attempts at most.
BACKGROUND_BACKOFF_CAP_SECONDS = 60.0

#: How long an agent container waits for the database before giving up on a call.
CONTAINER_DB_RETRY_SECONDS = 30.0

_CONNECT_ERRORS = (
    OSError,
    asyncpg.exceptions.OperatorInterventionError,
    asyncpg.exceptions.TooManyConnectionsError,
    asyncpg.exceptions.PostgresConnectionError,
)


class DatabaseUnavailableError(Exception):
    """The database could not be reached."""


def guard_connect(engine: AsyncEngine, *, retry_for: float) -> AsyncEngine:
    """Raise :class:`DatabaseUnavailableError` when ``engine`` cannot connect,
    after retrying for ``retry_for`` seconds."""

    @event.listens_for(engine.sync_engine, "do_connect")
    def _connect(dialect: Any, _record: Any, cargs: Any, cparams: Any) -> Any:
        deadline = time.monotonic() + retry_for
        for delay in backoff(cap=max(retry_for, 1.0)):
            try:
                return dialect.connect(*cargs, **cparams)
            except _CONNECT_ERRORS as e:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise DatabaseUnavailableError(f"Database unreachable: {e}") from e
                logger.warning("Database unreachable while connecting, retrying: %s", e)
                # The connect runs inside the engine's greenlet, so this waits
                # without blocking the event loop.
                await_only(asyncio.sleep(min(delay, left)))
        raise AssertionError("backoff never ends")

    return engine


def is_connection_error(exc: BaseException) -> bool:
    """Whether ``exc`` means the database was unreachable, so a retry may succeed."""
    if isinstance(exc, DatabaseUnavailableError | OperationalError | InterfaceError):
        return True
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def backoff(cap: float) -> Iterator[float]:
    """Delays of 1 s, doubling up to ``cap``."""
    delay = 1.0
    while True:
        yield delay
        delay = min(delay * 2, cap)


async def retry_async[T](call: Callable[[], Awaitable[T]], *, within: float, what: str) -> T:
    """Await ``call``, retrying connection errors until ``within`` seconds have passed."""
    deadline = time.monotonic() + within
    for delay in backoff(cap=within or 1.0):
        try:
            return await call()
        except Exception as e:
            left = deadline - time.monotonic()
            if not is_connection_error(e) or left <= 0:
                raise
            logger.warning("Database unreachable while %s, retrying: %s", what, e)
            await asyncio.sleep(min(delay, left))
    raise AssertionError("backoff never ends")


def retry_sync[T](call: Callable[[], T], *, what: str) -> T:
    """Call ``call``, retrying connection errors until it succeeds."""
    for delay in backoff(cap=BACKGROUND_BACKOFF_CAP_SECONDS):
        try:
            return call()
        except Exception as e:
            if not is_connection_error(e):
                raise
            logger.error("Database unreachable while %s, retrying in %.0f s: %s", what, delay, e)
            time.sleep(delay)
    raise AssertionError("backoff never ends")
