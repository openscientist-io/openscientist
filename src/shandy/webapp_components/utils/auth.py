"""Authentication utilities for the web application."""

import logging
import os
from functools import wraps
from typing import Callable

from nicegui import app, ui

logger = logging.getLogger(__name__)

# Authentication settings
DISABLE_AUTH = os.getenv("DISABLE_AUTH", "false").lower() == "true"


def is_dev_mode() -> bool:
    """Check if running in development mode."""
    return os.getenv("SHANDY_DEV_MODE", "false").lower() == "true"


if DISABLE_AUTH:
    logger.warning("Authentication is DISABLED! Anyone can access this app.")
    logger.warning("Set DISABLE_AUTH=false in .env to re-enable authentication.")


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
        if DISABLE_AUTH:
            return func(*args, **kwargs)

        # Check if authenticated
        if not app.storage.user.get("authenticated", False):
            ui.navigate.to("/login")
            return

        return func(*args, **kwargs)

    return wrapper
