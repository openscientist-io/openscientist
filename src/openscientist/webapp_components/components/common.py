"""Shared UI helpers, colours and state elements for the web app."""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nicegui import ui
from sqlalchemy import select

from openscientist.database.models import User
from openscientist.database.session import get_admin_session
from openscientist.job.types import JobStatus

logger = logging.getLogger(__name__)


OPENSCIENTIST_GITHUB_URL = "https://github.com/openscientist-io/openscientist"
OPENSCIENTIST_DOCS_URL = "https://docs.openscientist.io"
OPENSCIENTIST_RELEASE_URL = "https://github.com/openscientist-io/openscientist/releases/latest"
OPENSCIENTIST_PAPER_URL: str | None = (
    "https://www.medrxiv.org/content/10.64898/2026.03.15.26348338v1"
)


def get_project_resource_links() -> list[tuple[str, str]]:
    """Return the verified project links we expose in the UI."""
    links: list[tuple[str, str]] = [
        ("GitHub", OPENSCIENTIST_GITHUB_URL),
        ("Docs", OPENSCIENTIST_DOCS_URL),
    ]
    if OPENSCIENTIST_PAPER_URL:
        links.append(("Paper", OPENSCIENTIST_PAPER_URL))
    links.append(("Latest Release", OPENSCIENTIST_RELEASE_URL))
    return links


def render_project_resource_links() -> None:
    """Render a compact row of project resource links."""
    with ui.column().classes("w-full items-center gap-2"):
        ui.label("Resources").classes("text-xs font-semibold uppercase tracking-wide text-cyan-600")
        with ui.row().classes("w-full justify-center gap-2 flex-wrap"):
            for label, target in get_project_resource_links():
                ui.link(label, target=target, new_tab=target.startswith("http")).classes(
                    "no-underline rounded-full border border-cyan-200 bg-cyan-50 px-3 py-1 "
                    "text-sm font-medium text-cyan-700 hover:bg-cyan-100"
                )


def format_relative_time(dt: datetime | None) -> str:
    """Format datetime as relative time (e.g., '2 hours ago')."""
    if dt is None:
        return "-"

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


AsyncCallback = Callable[[dict[str, Any]], Awaitable[None]]

#: The vendored Font Awesome stylesheets, served same-origin under ``/fa``, for
#: a page that shows ``fa-solid`` icons.
FONTAWESOME_HEAD_HTML = (
    '<link rel="stylesheet" href="/fa/css/fontawesome.min.css">\n'
    '<link rel="stylesheet" href="/fa/css/solid.min.css">'
)


STATUS_COLORS = {
    JobStatus.PENDING: "gray",
    JobStatus.QUEUED: "blue",
    JobStatus.RUNNING: "teal",
    JobStatus.GENERATING_REPORT: "teal",
    JobStatus.COMPLETED: "green",
    JobStatus.FAILED: "red",
    JobStatus.CANCELLED: "gray",
    JobStatus.AWAITING_FEEDBACK: "orange",
}


CATEGORY_COLORS: dict[str, str] = {
    "analysis": "blue",
    "methodology": "purple",
    "statistics": "green",
    "biology": "teal",
    "chemistry": "orange",
    "bioinformatics": "cyan",
    "machine-learning": "indigo",
    "data-science": "violet",
    "genomics": "pink",
    "metabolomics": "amber",
    "proteomics": "lime",
}


def get_category_color(category: str) -> str:
    """Quasar/Tailwind color for a skill category, case-insensitive."""
    return CATEGORY_COLORS.get(category.lower(), "gray")


def render_stat_badges(
    stats: list[tuple[str, str | int, str]],
    icon_map: dict[str, str] | None = None,
) -> ui.row:
    """Render a compact row of stat badges that wraps on narrow screens.

    Returns the row so a caller can append a small sibling, like the
    run-environment info icon.
    """
    default_icons = {
        "Total": "list",
        "Total Jobs": "list",
        "Running": "play_circle",
        "Completed": "check_circle",
        "Failed": "error",
        "Status": "info",
        "Progress": "trending_up",
        "Findings": "lightbulb",
        "Papers": "article",
        "Papers Reviewed": "article",
        "Skills": "school",
        "Model": "psychology",
        "Agent": "smart_toy",
        "Provider": "cloud",
        "Spend": "payments",
        "Total Cost": "payments",
    }
    icons = {**default_icons, **(icon_map or {})}

    with ui.row().classes("w-full gap-1 flex-wrap items-center mb-1") as badges_row:
        for label, value, color in stats:
            badge_color = color if color else "gray"
            icon = icons.get(label, "tag")
            with (
                ui.badge(color=badge_color).props("outline").classes("px-2 py-0.5 text-xs"),
                ui.row().classes("items-center gap-1"),
            ):
                ui.icon(icon, size="xs")
                ui.label(f"{label}: {value}").classes("font-medium")
    return badges_row


def render_empty_state(message: str) -> None:
    """Render a styled empty state message."""
    ui.label(message).classes("text-gray-500 text-center p-8")


def render_permission_badge_slot() -> str:
    """Quasar slot template rendering the permission level as a colored badge."""
    return r"""
        <q-td :props="props">
            <q-badge :color="props.row.permission === 'edit' ? 'orange' : 'blue'">
                {{ props.row.permission }}
            </q-badge>
        </q-td>
    """


def render_not_found_state(
    title: str,
    message: str,
    back_label: str = "Go Back",
    back_url: str | None = None,
    icon: str = "search_off",
) -> None:
    """Render a styled 404/not found state with an optional back button."""
    with ui.column().classes("w-full items-center py-16"):
        ui.icon(icon, size="xl").classes("text-gray-300 mb-4")
        ui.label(title).classes("text-h5 font-bold text-gray-600 mb-2")
        ui.label(message).classes("text-gray-500 mb-4")
        if back_url:
            ui.button(
                back_label,
                on_click=lambda: ui.navigate.to(back_url),
                icon="arrow_back",
            ).props("color=primary")


def render_error_state(
    title: str,
    message: str,
    back_label: str = "Go Back",
    back_url: str | None = None,
    icon: str = "error",
) -> None:
    """Render a styled error state with an optional back button."""
    with ui.column().classes("w-full items-center py-16"):
        ui.icon(icon, size="xl").classes("text-red-300 mb-4")
        ui.label(title).classes("text-h5 font-bold text-red-600 mb-2")
        ui.label(message).classes("text-gray-500 mb-4")
        if back_url:
            ui.button(
                back_label,
                on_click=lambda: ui.navigate.to(back_url),
                icon="arrow_back",
            ).props("color=primary")


def render_dialog_actions(
    on_confirm: Callable[[], None | Awaitable[None]],
    on_cancel: Callable[[], None | Awaitable[None]],
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    confirm_props: str = "color=primary",
) -> None:
    """Render standard dialog action buttons (Cancel/Confirm)."""
    with ui.row().classes("w-full justify-end gap-2 mt-4"):
        ui.button(cancel_label, on_click=on_cancel).props("flat")
        ui.button(confirm_label, on_click=on_confirm).props(confirm_props)


def make_action_button_slot(
    label: str,
    event_name: str,
    icon: str | None = None,
    color: str = "primary",
) -> str:
    """Quasar slot template for an icon-only action button whose tooltip is
    ``label``, emitting ``event_name`` with the row."""
    icon_attr = f'icon="{icon}"' if icon else ""
    return f"""
<q-td :props="props">
    <q-btn
        flat
        dense
        size="sm"
        color="{color}"
        {icon_attr}
        @click="$parent.$emit('{event_name}', props.row)"
    >
        <q-tooltip>{label}</q-tooltip>
    </q-btn>
</q-td>
"""


async def render_user_search(
    on_select: AsyncCallback,
    placeholder: str = "Search by email or name",
    action_label: str = "Select",
    action_icon: str = "check",
) -> tuple[ui.input, ui.column]:
    """Render a user search input with results list."""
    search_input = ui.input(
        label=placeholder,
        placeholder="Type to search...",
    ).classes("w-full mb-4")

    results_container = ui.column().classes("w-full max-h-48 overflow-y-auto")

    async def search_users() -> None:
        """Search for users by email or name."""
        query = search_input.value
        if not query or len(query) < 2:
            results_container.clear()
            return

        try:
            # Use admin session to search all users
            async with get_admin_session() as session:
                stmt = (
                    select(User)
                    .where(User.email.ilike(f"%{query}%") | User.name.ilike(f"%{query}%"))
                    .limit(10)
                )
                result = await session.execute(stmt)
                users = result.scalars().all()

            results_container.clear()
            with results_container:
                if not users:
                    ui.label("No users found").classes("text-gray-500 text-sm p-2")
                else:
                    for user in users:
                        with ui.row().classes(
                            "w-full items-center gap-2 p-2 hover:bg-gray-100 cursor-pointer"
                        ):
                            ui.label(f"{user.name} ({user.email})").classes("flex-grow text-sm")

                            async def select_user(u: Any = user) -> None:
                                user_data = {
                                    "id": u.id,
                                    "name": u.name,
                                    "email": u.email,
                                }
                                await on_select(user_data)

                            ui.button(
                                action_label,
                                icon=action_icon,
                                on_click=select_user,
                            ).props("size=sm flat")

        except Exception as e:
            logger.error("Failed to search users: %s", e, exc_info=True)
            results_container.clear()
            with results_container:
                ui.label("Search failed").classes("text-red-500 text-sm")

    async def _on_search_change(_e: Any) -> None:
        await search_users()

    search_input.on_value_change(_on_search_change)

    return search_input, results_container


def _inject_density_styles() -> None:
    """Register the shared density CSS: compact cards and a wrapped environment tooltip.

    Call exactly once at app bootstrap (same reason as ``_inject_pubmed_badge_styles``).
    """
    ui.add_head_html(
        """
        <style>
        .os-density-card .q-card__section {
            padding: 8px 12px;
        }
        .os-env-tooltip {
            max-width: 420px;
            white-space: normal;
        }
        .os-compact-upload .q-uploader__header-content {
            padding: 4px 8px;
        }
        .os-compact-upload {
            min-height: 0;
        }
        .os-compact-upload .q-uploader__list {
            min-height: 0;
        }
        .os-compact-upload .q-uploader__list:empty {
            display: none;
        }
        /* Colour, not opacity, so the tooltip child keeps its own text colour. */
        .q-tab.os-tab-disabled,
        .q-tab.os-tab-disabled .q-tab__label,
        .q-tab.os-tab-disabled .q-icon {
            color: rgba(0, 0, 0, 0.35);
        }
        </style>
        """,
        shared=True,
    )


def _inject_thinking_status_styles() -> None:
    """Register thinking-status CSS into the app-wide page head.

    Call exactly once at app bootstrap (same reason as ``_inject_pubmed_badge_styles``).
    """
    ui.add_head_html(
        """
        <style>
        .thinking-label {
            color: #0891b2;
            font-size: 12px;
            font-weight: 500;
            animation: openscientist-pulse-text 1.5s ease-in-out infinite;
        }
        @keyframes openscientist-pulse-text {
            0%, 100% { opacity: 0.6; }
            50% { opacity: 1; }
        }
        </style>
        """,
        shared=True,
    )


_ASSETS_DIR = Path(__file__).parent.parent.parent / "assets"

# Rendered inline (via ui.html) so SMIL animations work. <img> tags disable them.
OPENSCIENTIST_THINKING_SVG = (_ASSETS_DIR / "thinking.svg").read_text(encoding="utf-8")


def render_container_status_badge(status: str) -> None:
    """Render a Docker container status as a colored Quasar badge."""
    color_map = {
        "running": "green",
        "exited": "grey",
        "created": "blue",
        "restarting": "orange",
        "dead": "red",
        "removing": "red",
        "paused": "grey",
    }
    color = color_map.get(status, "grey")
    ui.badge(status, color=color).classes("px-2 py-1")


def format_uptime(seconds: float) -> str:
    """Format seconds as a human-readable uptime string (e.g., '2h 15m')."""
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


def render_thinking_status(status_text: str = "Thinking...") -> ui.element:
    """Render an animated OpenScientist thinking/status indicator."""
    with ui.row().classes(
        "items-center gap-3 py-3 px-4 bg-cyan-50 rounded-lg border border-cyan-200"
    ) as container:
        ui.html(OPENSCIENTIST_THINKING_SVG, sanitize=False).classes("w-6 h-6").style(
            "width:24px;height:24px;min-width:24px;min-height:24px;flex-shrink:0;"
        )
        ui.label(status_text).classes("text-cyan-700 italic thinking-label")

    return container
