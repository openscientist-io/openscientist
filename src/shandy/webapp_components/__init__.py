"""
Web application subpackage for SHANDY.

Contains modular UI components and utilities for the NiceGUI web interface.
"""

from shandy.webapp_components.error_handler import get_user_friendly_error
from shandy.webapp_components.ui_components import render_error_card, render_status_cell_slot

__all__ = [
    "get_user_friendly_error",
    "render_error_card",
    "render_status_cell_slot",
]
