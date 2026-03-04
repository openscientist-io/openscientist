"""Utilities for protecting NiceGUI operations from disconnected clients.

NiceGUI clients can be deleted during async operations (e.g., during auto-reload
or when users navigate away). This module provides guards to safely handle these
cases and prevent "Client has been deleted" warnings.

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
    """Check if the current NiceGUI client is still connected.

    Uses NiceGUI's internal `_deleted` flag which is the authoritative source.
    Returns False if the client is deleted or if we can't access the client.
    """
    try:
        client = ui.context.client
        return not getattr(client, "_deleted", True)
    except (AttributeError, RuntimeError):
        return False


def guard_client[F: Callable[..., Any]](func: F) -> F:
    """Decorator that skips execution if the client has been deleted.

    Use on timer callbacks, event handlers, and async functions that modify UI.

    Example:
        @guard_client
        def my_timer_callback():
            ui.label("Updated!")  # Safe - won't crash if client deleted

        @guard_client
        async def my_async_handler():
            result = await slow_operation()
            ui.label(result)  # Still need ClientGuard for re-checks after await
    """

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
        # Handle the awaitable
        if asyncio.iscoroutine(result):
            return await result
        return result

    if asyncio.iscoroutinefunction(func):
        return async_wrapper  # type: ignore[return-value]
    return sync_wrapper  # type: ignore[return-value]


class ClientGuard:
    """Reusable guard for async functions needing multiple check points.

    For async functions, the client can disconnect between await points.
    Use this class to re-check connection status after each await.

    Example:
        async def my_async_handler():
            guard = ClientGuard()
            if not guard.is_connected:
                return

            result = await long_operation()

            # Re-check after await - client may have disconnected
            if not guard.is_connected:
                return

            ui.label(result)  # Safe to modify UI
            guard.run_javascript("console.log('done')")  # Safe JS execution
    """

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
    """Execute JavaScript safely, checking client existence first.

    Standalone function for simple cases where you don't need a full ClientGuard.

    Example:
        safe_run_javascript("document.querySelector('.chat').scrollTop = 999")
    """
    if is_client_connected():
        try:
            ui.run_javascript(code)
        except Exception:
            logger.debug("JS execution failed (client likely disconnected)", exc_info=True)


def setup_timer_cleanup() -> list[ui.timer]:
    """Set up automatic timer cleanup on client disconnect.

    Returns a list to track active timers. Add timers to this list
    and they'll be deactivated when the client disconnects.

    Example:
        _active_timers = setup_timer_cleanup()

        # Create timers and track them
        my_timer = ui.timer(5.0, my_callback)
        _active_timers.append(my_timer)

        # Timers will be automatically cleaned up when client disconnects
    """
    active_timers: list[ui.timer] = []

    def cleanup() -> None:
        for timer in active_timers:
            try:
                timer.deactivate()
            except Exception:
                logger.debug("Timer deactivation failed (may already be inactive)", exc_info=True)

    ui.context.client.on_disconnect(cleanup)
    return active_timers
