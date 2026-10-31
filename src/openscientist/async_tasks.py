"""
Helpers for safe fire-and-forget asyncio task execution and sync/async bridging.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

_T = TypeVar("_T")

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()

_thread_runner = threading.local()


def create_background_task(
    coroutine: Coroutine[Any, Any, Any],
    *,
    name: str,
    logger: logging.Logger | None = None,
) -> asyncio.Task[Any]:
    """Create a background task and keep a strong reference until completion.

    The reference keeps the task from being garbage-collected before it finishes.
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

    The deadline lives inside the coroutine's own loop, so at ``timeout`` the
    coroutine is cancelled and its unwinding is awaited before ``TimeoutError``
    reaches the caller. It is cooperative, so bound the work itself (for a
    query, the database's ``statement_timeout``) when a hard wall is required.

    Only for threads without a running event loop. On a running loop it raises,
    and the caller must await the coroutine instead. Inside :func:`thread_loop`
    the coroutine runs on that loop, and otherwise on a new loop of its own.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        runner: asyncio.Runner | None = getattr(_thread_runner, "runner", None)
        if runner is not None:
            return runner.run(_bounded(coro, timeout))
        return asyncio.run(_bounded(coro, timeout))
    name = coro.__qualname__
    coro.close()
    raise RuntimeError(
        f"run_sync({name}) was called on a running event loop, which it would block. "
        "Await the coroutine instead."
    )


async def _bounded(coro: Coroutine[Any, Any, _T], timeout: float) -> _T:
    """Await the coroutine under the deadline, on the loop that runs it, so a
    cancellation reaches it and its cleanup completes before we return."""
    return await asyncio.wait_for(coro, timeout)


@contextmanager
def thread_loop() -> Iterator[None]:
    """Run this thread's :func:`run_sync` calls on one loop until the block ends,
    so their sessions share that loop's pooled connections. Closing the loop at
    the end disposes its engines."""
    with asyncio.Runner() as runner:
        _thread_runner.runner = runner
        try:
            yield
        finally:
            del _thread_runner.runner
