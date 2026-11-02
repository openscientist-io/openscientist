"""Guards against NiceGUI operations on clients that get deleted mid-operation.

See: https://github.com/zauberzeug/nicegui/issues/3028
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

from nicegui import ui

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nicegui import Client


def is_client_connected() -> bool:
    """Check if the current NiceGUI client is still connected."""
    try:
        client = ui.context.client
        return not getattr(client, "_deleted", True)
    except (AttributeError, RuntimeError):
        return False


def guard_client[F: Callable[..., Any]](func: F) -> F:
    """Decorator that skips execution if the client has been deleted."""

    @wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if not is_client_connected():
            return None
        return func(*args, **kwargs)

    @wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        if not is_client_connected():
            return None
        result = func(*args, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result

    if asyncio.iscoroutinefunction(func):
        return async_wrapper  # type: ignore[return-value]
    return sync_wrapper  # type: ignore[return-value]


class ClientGuard:
    """Re-check `is_connected` after each await. The client may disconnect in between."""

    _client: Client | None

    def __init__(self) -> None:
        try:
            self._client = ui.context.client
        except (AttributeError, RuntimeError):
            self._client = None

    @property
    def is_connected(self) -> bool:
        """Check if the client is still connected."""
        if self._client is None:
            return False
        return not getattr(self._client, "_deleted", True)

    def run_javascript(self, code: str) -> None:
        """Run JavaScript only if client is still connected."""
        if self.is_connected:
            try:
                ui.run_javascript(code)
            except Exception:
                logger.debug("JS execution failed (client likely disconnected)", exc_info=True)


def safe_run_javascript(code: str) -> None:
    """Execute JavaScript safely, checking client existence first."""
    if is_client_connected():
        try:
            ui.run_javascript(code)
        except Exception:
            logger.debug("JS execution failed (client likely disconnected)", exc_info=True)


def setup_timer_cleanup() -> list[ui.timer]:
    """Deactivate the returned timers when the client disconnects."""
    active_timers: list[ui.timer] = []

    def cleanup() -> None:
        for timer in active_timers:
            try:
                timer.deactivate()
            except Exception:
                logger.debug("Timer deactivation failed (may already be inactive)", exc_info=True)

    ui.context.client.on_disconnect(cleanup)
    return active_timers
