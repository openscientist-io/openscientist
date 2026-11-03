"""Utility modules for the web application."""

from openscientist.webapp_components.utils.auth import is_dev_mode
from openscientist.webapp_components.utils.client_guard import (
    ClientGuard,
    guard_client,
    is_client_connected,
    safe_run_javascript,
    setup_timer_cleanup,
)
from openscientist.webapp_components.utils.events import get_event_value
from openscientist.webapp_components.utils.session import (
    add_uploaded_file,
    clear_uploaded_files,
    get_uploaded_files,
)

__all__ = [
    "ClientGuard",
    "add_uploaded_file",
    "clear_uploaded_files",
    "get_event_value",
    "get_uploaded_files",
    "guard_client",
    "is_client_connected",
    "is_dev_mode",
    "safe_run_javascript",
    "setup_timer_cleanup",
]
