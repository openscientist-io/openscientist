"""Guards against NiceGUI operations on clients that get deleted mid-operation.

See: https://github.com/zauberzeug/nicegui/issues/3028
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

from nicegui import ui

from openscientist.auth.middleware import go_to_login, session_still_valid_for

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


def session_checked_timer(
    interval: float, callback: Callable[[], Awaitable[None]], user_id: str
) -> ui.timer:
    """A ``ui.timer`` that re-checks the page's session before every tick.

    The callback runs only while the session the page was built with is
    still valid and still belongs to ``user_id``, the user the page was
    built for. Once it is not, the timer deactivates itself and sends the
    client to the login page instead of running the callback.
    """

    @wraps(callback)
    @guard_client
    async def checked_tick() -> None:
        if not await session_still_valid_for(user_id):
            timer.deactivate()
            go_to_login()
            return
        await callback()

    timer = ui.timer(interval, checked_tick)
    return timer


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
