"""Authentication utilities for the web application."""

import logging

from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


def is_dev_mode() -> bool:
    """Check if running in development mode."""
    return get_settings().dev.dev_mode
