"""Utility modules for the web application."""

from shandy.webapp_components.utils.auth import is_dev_mode
from shandy.webapp_components.utils.client_guard import (
    ClientGuard,
    guard_client,
    is_client_connected,
    safe_run_javascript,
    setup_timer_cleanup,
)
from shandy.webapp_components.utils.session import (
    add_uploaded_file,
    clear_uploaded_files,
    get_uploaded_files,
)
from shandy.webapp_components.utils.transcript_parser import (
    get_action_description,
    parse_transcript_actions,
)

__all__ = [
    "is_dev_mode",
    "get_uploaded_files",
    "add_uploaded_file",
    "clear_uploaded_files",
    "get_action_description",
    "parse_transcript_actions",
    # Client guard utilities
    "is_client_connected",
    "guard_client",
    "ClientGuard",
    "safe_run_javascript",
    "setup_timer_cleanup",
]
