"""
Helpers for safe fire-and-forget asyncio task execution.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

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
