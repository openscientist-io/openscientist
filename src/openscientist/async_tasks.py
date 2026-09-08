"""
Helpers for safe fire-and-forget asyncio task execution and sync/async bridging.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

_T = TypeVar("_T")

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def create_background_task(
    coroutine: Coroutine[Any, Any, Any],
    *,
    name: str,
    logger: logging.Logger | None = None,
) -> asyncio.Task[Any]:
    """
    Create a background task and keep a strong reference until completion.

    This avoids tasks being garbage-collected before they finish.

    Args:
        coroutine: Awaitable to execute in the background.
        name: Task name used for observability and error logs.
        logger: Logger to report uncaught task exceptions. If omitted, task
            exceptions are not logged here.

    Returns:
        The created asyncio task.
    """
    task = asyncio.create_task(coroutine, name=name)
    _BACKGROUND_TASKS.add(task)

    def _on_done(done_task: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(done_task)
        if logger is None or done_task.cancelled():
            return
        error = done_task.exception()
        if error is not None:
            logger.warning("Background task %s failed: %s", done_task.get_name(), error)

    task.add_done_callback(_on_done)
    return task


def run_sync(coro: Coroutine[Any, Any, _T], *, timeout: float = 30) -> _T:
    """Run an async coroutine from synchronous code, bounded by ``timeout``.

    The deadline lives inside the coroutine's own loop, so it cancels the work
    rather than abandoning it: at ``timeout`` the coroutine is cancelled and
    its unwinding is awaited before ``TimeoutError`` reaches the caller. It is
    therefore cooperative, and a coroutine that refuses to unwind still delays
    the caller. Bound the work itself (for a query, the database's own
    ``statement_timeout``) when a hard wall is required.

    ``asyncio.run`` owns the loop in both paths, so task cancellation, async
    generator shutdown and executor shutdown all happen as usual.

    Args:
        coro: The coroutine to execute.
        timeout: Seconds before the coroutine is cancelled.

    Returns:
        The coroutine's return value.

    Raises:
        TimeoutError: The deadline passed and the coroutine was cancelled.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_bounded(coro, timeout))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, _bounded(coro, timeout)).result()


async def _bounded(coro: Coroutine[Any, Any, _T], timeout: float) -> _T:
    """Await the coroutine under the deadline, on the loop that runs it, so a
    cancellation reaches it and its cleanup completes before we return."""
    return await asyncio.wait_for(coro, timeout)
