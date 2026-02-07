"""Authentication utilities for the web application."""

import logging
import os
from functools import wraps
from typing import Callable

import bcrypt
from nicegui import app, ui

logger = logging.getLogger(__name__)

# Authentication settings
DISABLE_AUTH = os.getenv("DISABLE_AUTH", "false").lower() == "true"
PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "").encode()

if DISABLE_AUTH:
    logger.warning("Authentication is DISABLED! Anyone can access this app.")
    logger.warning("Set DISABLE_AUTH=false in .env to re-enable authentication.")


def check_password(password: str) -> bool:
    """
    Check if password matches the hash.

    Args:
        password: Password to check

    Returns:
        True if password is correct or no password is set
    """
    if not PASSWORD_HASH:
        return True  # No password set, allow access
    try:
        return bcrypt.checkpw(password.encode(), PASSWORD_HASH)
    except (ValueError, TypeError) as e:
        logger.error("Password check failed: %s", e)
        return False


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
