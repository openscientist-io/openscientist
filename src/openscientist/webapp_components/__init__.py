"""
Web application subpackage for OpenScientist.

Contains modular UI components and utilities for the NiceGUI web interface.
"""

from openscientist.webapp_components.components.banners import render_error_card
from openscientist.webapp_components.components.job_table import render_status_cell_slot
from openscientist.webapp_components.error_handler import get_user_friendly_error

__all__ = [
    "get_user_friendly_error",
    "render_error_card",
    "render_status_cell_slot",
]
