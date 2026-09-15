"""
Text formatting and rendering UI components for OpenScientist web interface.

Provides small, self-contained helpers for formatting human-readable time
strings and rendering justified paragraph text.
"""

import html
from datetime import UTC, datetime

from nicegui import ui


def format_relative_time(dt: datetime | None) -> str:
    """
    Format datetime as relative time (e.g., '2 hours ago').

    Args:
        dt: Datetime to format, or None

    Returns:
        Human-readable relative time string, or '-' if dt is None
    """
    if dt is None:
        return "-"

    # Ensure dt is timezone-aware (assume UTC if naive)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    now = datetime.now(UTC)
    delta = now - dt

    seconds = delta.total_seconds()

    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if seconds < 604800:
        days = int(seconds / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    if seconds < 2592000:
        weeks = int(seconds / 604800)
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = int(seconds / 2592000)
    return f"{months} month{'s' if months != 1 else ''} ago"


def render_justified_text(
    text: str,
    text_classes: str = "text-sm text-gray-700",
) -> None:
    """
    Render text as a justified paragraph for better readability.

    Uses text-align: justify with automatic hyphenation for clean
    paragraph formatting in large text blocks.

    Args:
        text: The text to render
        text_classes: CSS classes for styling (color, size, etc.)
    """
    if not text:
        return

    ui.html(
        f'<p class="{text_classes}" style="text-align:justify;hyphens:auto;'
        f'text-justify:inter-word;margin:0;">{html.escape(text)}</p>'
    )


def format_uptime(seconds: float) -> str:
    """Format seconds as a human-readable uptime string.

    Examples:
        >>> format_uptime(30)
        '30s'
        >>> format_uptime(90)
        '1m 30s'
        >>> format_uptime(8100)
        '2h 15m'
    """
    if seconds < 0:
        return "0s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m" if mins else f"{hours}h"
