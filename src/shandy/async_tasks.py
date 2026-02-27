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
    """Run an async coroutine from synchronous code.

    When called from within a running event loop (e.g., NiceGUI handlers),
    runs the coroutine in a separate thread with its own event loop.
    Otherwise, uses ``asyncio.run()`` directly.

    Args:
        coro: The coroutine to execute.
        timeout: Maximum wait time in seconds when running in a thread.

    Returns:
        The coroutine's return value.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result(timeout=timeout)
