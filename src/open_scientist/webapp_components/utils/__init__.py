"""Utility modules for the web application."""

from open_scientist.webapp_components.utils.auth import is_dev_mode
from open_scientist.webapp_components.utils.client_guard import (
    ClientGuard,
    guard_client,
    is_client_connected,
    safe_run_javascript,
    setup_timer_cleanup,
)
from open_scientist.webapp_components.utils.session import (
    add_uploaded_file,
    clear_uploaded_files,
    get_uploaded_files,
)
from open_scientist.webapp_components.utils.transcript_parser import (
    get_action_description,
    parse_transcript_actions,
)

__all__ = [
    "ClientGuard",
    "add_uploaded_file",
    "clear_uploaded_files",
    "get_action_description",
    "get_uploaded_files",
    "guard_client",
    "is_client_connected",
    "is_dev_mode",
    "parse_transcript_actions",
    "safe_run_javascript",
    "setup_timer_cleanup",
]
