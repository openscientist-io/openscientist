"""Authentication utilities for the web application."""

import logging
from functools import wraps
from typing import Callable

from nicegui import app, ui

from shandy.settings import get_settings

logger = logging.getLogger(__name__)


def is_auth_disabled() -> bool:
    """Check if authentication is disabled."""
    return get_settings().auth.disable_auth


def is_dev_mode() -> bool:
    """Check if running in development mode."""
    return get_settings().dev.dev_mode


def require_auth(func: Callable) -> Callable:
    """
    Decorator to require authentication for a page.

    Args:
        func: Page function to protect

    Returns:
        Wrapped function that checks authentication
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        # Skip if auth is disabled
        if is_auth_disabled():
            return func(*args, **kwargs)

        # Check if authenticated
        if not app.storage.user.get("authenticated", False):
            ui.navigate.to("/login")
            return

        return func(*args, **kwargs)

    return wrapper
