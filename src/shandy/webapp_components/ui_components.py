"""
Reusable UI components for SHANDY web interface.

Provides UI rendering functions for error displays, status badges,
page headers, and other common interface elements.
"""

import html
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Dict

from nicegui import ui

from shandy.job_manager import JobInfo, JobStatus

# Type alias for async callbacks
AsyncCallback = Callable[[dict], Awaitable[None]]

# Status color mappings
STATUS_COLORS = {
    JobStatus.PENDING: "gray",
    JobStatus.QUEUED: "blue",
    JobStatus.RUNNING: "yellow",
    JobStatus.COMPLETED: "green",
    JobStatus.FAILED: "red",
    JobStatus.CANCELLED: "gray",
    JobStatus.AWAITING_FEEDBACK: "orange",
}

# Status icons (unicode/emoji)
STATUS_ICONS = {
    JobStatus.PENDING: "○",
    JobStatus.QUEUED: "⟳",
    JobStatus.RUNNING: "▶",
    JobStatus.COMPLETED: "✓",
    JobStatus.FAILED: "✗",
    JobStatus.CANCELLED: "⊗",
    JobStatus.AWAITING_FEEDBACK: "⏸",
}


def _get_pubmed_badge_html(pmid: str) -> str:
    """
    Generate HTML for a PubMed badge with logo and PMID.

    Creates an inline badge element with:
    - PubMed logo (inline SVG)
    - PMID number
    - Tooltip explaining the link
    - Opens in new tab (via CSS class, handled by injected script)

    Args:
        pmid: The PubMed ID number

    Returns:
        HTML string for the badge element
    """
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    tooltip = f"Visit PubMed page for PMID {pmid}"

    # PubMed-style icon: stylized "P" in a rounded square
    # Using a simple, clean design that's recognizable at small sizes
    pubmed_icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" '
        'class="pubmed-icon">'
        '<rect x="1" y="1" width="14" height="14" rx="2" fill="#326599"/>'
        '<text x="8" y="12" text-anchor="middle" '
        'style="font-size:11px;font-weight:bold;font-family:Arial,sans-serif;fill:white;">'
        "P</text></svg>"
    )

    return (
        f'<a href="{url}" rel="noopener noreferrer" '
        f'title="{tooltip}" class="pubmed-badge">'
        f"{pubmed_icon}{html.escape(pmid)}</a>"
    )


def _inject_pubmed_badge_styles() -> None:
    """Inject CSS and JS for PubMed badges into page head (idempotent)."""
    # Using add_head_html with shared=True ensures this is only added once per client
    ui.add_head_html(
        """
        <style>
        .pubmed-badge {
            display: inline-flex;
            align-items: center;
            padding: 1px 6px 1px 4px;
            margin: 0 2px;
            background-color: #e8f4f8;
            border: 1px solid #326599;
            border-radius: 4px;
            text-decoration: none;
            color: #326599;
            font-size: 0.85em;
            font-weight: 500;
            transition: background-color 0.2s;
        }
        .pubmed-badge:hover {
            background-color: #cce5ed;
        }
        .pubmed-icon {
            width: 14px;
            height: 14px;
            vertical-align: middle;
            margin-right: 3px;
        }
        </style>
        <script>
        // Make all PubMed links open in new tab (event delegation)
        if (!window._pubmedClickHandlerAdded) {
            window._pubmedClickHandlerAdded = true;
            document.addEventListener('click', function(e) {
                var link = e.target.closest('.pubmed-badge');
                if (link) {
                    e.preventDefault();
                    window.open(link.href, '_blank', 'noopener,noreferrer');
                }
            });
        }
        </script>
        """,
        shared=True,
    )


def render_pmid_badge(pmid: str) -> None:
    """
    Render a single PMID as a clickable PubMed badge.

    Creates an inline badge element with PubMed logo, PMID number,
    tooltip, and link that opens in a new tab.

    Use this for standalone PMID display (e.g., in literature lists).
    For PMIDs embedded in text, use render_text_with_pmid_links() instead.

    Args:
        pmid: The PubMed ID number (just the numeric part)
    """
    # Inject CSS/JS for badges into page head
    _inject_pubmed_badge_styles()

    # Render badge as inline HTML
    badge_html = _get_pubmed_badge_html(pmid)
    ui.html(badge_html)


def render_text_with_pmid_links(
    text: str,
    text_classes: str = "text-sm text-gray-700",
) -> None:
    """
    Render text with PMID references converted to clickable badges.

    Parses text for PMID patterns and renders them as clickable PubMed badges
    with logo, tooltip, and link to PubMed. Supports both single PMIDs and
    comma-separated lists.

    Patterns matched:
    - "PMID: 12345678"
    - "PMID 12345678"
    - "PMID: 12345678, 87654321, 11111111"

    Args:
        text: The text containing PMID references
        text_classes: CSS classes for the text container
    """
    if not text:
        return

    # Pattern matches "PMID" followed by optional colon/space, then comma-separated numbers
    # Case-insensitive, captures the prefix and the number list separately
    pattern = re.compile(r"(PMID[:\s]+)(\d{1,8}(?:\s*,\s*\d{1,8})*)", re.IGNORECASE)

    # Build HTML with text segments and badges
    result_parts = []
    last_end = 0

    for match in pattern.finditer(text):
        # Add text before this match (escaped)
        if match.start() > last_end:
            result_parts.append(html.escape(text[last_end : match.start()]))

        # Extract PMIDs (skip the prefix like "PMID: " since badges are self-explanatory)
        pmid_list = match.group(2)

        # Split and create badges for each PMID
        pmids = [p.strip() for p in pmid_list.split(",")]
        pmid_badges = [_get_pubmed_badge_html(pmid) for pmid in pmids]
        result_parts.append(" ".join(pmid_badges))

        last_end = match.end()

    # Add remaining text after last match (escaped)
    if last_end < len(text):
        result_parts.append(html.escape(text[last_end:]))

    # If no PMIDs found, render as plain label
    if not result_parts:
        ui.label(text).classes(text_classes)
        return

    # Inject CSS/JS for badges into page head
    _inject_pubmed_badge_styles()

    # Render as HTML to support inline badges
    html_content = "".join(result_parts)
    ui.html(f'<span class="{text_classes}">{html_content}</span>')


def render_error_card(error_info: Dict, job_info: JobInfo, job_dir: Path) -> None:
    """
    Render a user-friendly error card with tiered disclosure.

    Creates a visually prominent error display with:
    - User-friendly title and message at the top
    - Actionable steps in bullet list
    - Collapsible sections for additional context and technical details

    Args:
        error_info: Dict from get_user_friendly_error() with keys:
                   category, title, message, extracted_error, steps, raw, contact_admin
        job_info: JobInfo object for the failed job
        job_dir: Path to job directory (for potential log access)
    """
    with ui.card().classes("w-full bg-red-50 border-2 border-red-300 mb-4 p-6"):
        # Header with icon and title
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("error", size="lg").classes("text-red-600")
            ui.label(error_info["title"]).classes("text-h6 font-bold text-red-800")

        # User-friendly message
        ui.label(error_info["message"]).classes("text-red-700 mb-4")

        # Actionable steps
        if error_info.get("steps"):
            ui.label("How to resolve:").classes("font-bold text-red-800 mb-2")
            with ui.column().classes("gap-2 mb-4 pl-2"):
                for step in error_info["steps"]:
                    with ui.row().classes("gap-2 items-start"):
                        ui.label("•").classes("text-red-600 font-bold")
                        ui.label(step).classes("text-sm text-red-700")

        # Contact admin button if needed
        if error_info.get("contact_admin"):
            ui.label("This error requires administrator assistance.").classes(
                "text-sm text-red-600 italic mt-2 mb-3 px-2 py-2 bg-red-100 rounded"
            )

        # Collapsible: What happened
        with ui.expansion("What happened?", icon="info").classes(
            "w-full mt-3 border border-red-200 rounded"
        ):
            with ui.column().classes("gap-2 p-3"):
                ui.label(f"Error Category: {error_info['category'].title()}").classes(
                    "text-sm font-bold text-gray-800"
                )
                ui.label(f"Job Status: {job_info.status.value}").classes("text-sm text-gray-700")
                ui.label(
                    f"Iterations Completed: {job_info.iterations_completed}/{job_info.max_iterations}"
                ).classes("text-sm text-gray-700")
                if job_info.failed_at:
                    ui.label(f"Failed At: {job_info.failed_at[:19]}").classes(
                        "text-sm text-gray-700"
                    )

                # Extracted error message
                if error_info.get("extracted_error"):
                    ui.label("Error Message:").classes("text-sm font-bold mt-3 text-gray-800")
                    with ui.element("div").classes("w-full overflow-hidden"):
                        ui.label(error_info["extracted_error"]).classes(
                            "text-sm bg-white p-3 rounded border border-red-200 text-gray-700"
                        ).style("word-break: break-word; overflow-wrap: break-word;")

        # Collapsible: Technical details
        with ui.expansion("Technical Details", icon="code").classes(
            "w-full mt-2 border border-red-200 rounded overflow-hidden"
        ):
            with ui.column().classes("gap-2 p-3 w-full"):
                with ui.row().classes("items-center justify-between w-full mb-2 flex-nowrap"):
                    ui.label("Raw Error Output:").classes("text-sm font-bold text-gray-800")

                    def copy_to_clipboard():
                        ui.run_javascript(
                            f"""
                            navigator.clipboard.writeText({repr(error_info["raw"])});
                        """
                        )
                        ui.notify("Error message copied to clipboard", type="positive")

                    with (
                        ui.button(icon="content_copy", on_click=copy_to_clipboard)
                        .props("flat dense color=primary size=sm")
                        .classes("flex-shrink-0")
                    ):
                        ui.tooltip("Copy to clipboard")

                # Wrap code block in container to prevent overflow
                with ui.element("div").classes("w-full overflow-x-auto"):
                    ui.code(error_info["raw"], language="text").classes(
                        "text-xs max-h-[300px] overflow-y-auto p-2 bg-gray-50 rounded"
                    ).style("word-break: break-word; white-space: pre-wrap; max-width: 100%;")

                # Link to logs if available
                log_file = job_dir / "orchestrator.log"
                if log_file.exists():
                    ui.label("Check the orchestrator.log file for complete details.").classes(
                        "text-xs text-gray-600 mt-2"
                    )


def render_config_error_banner(
    provider_name: str,
    config_errors: list[str],
    show_back_button: bool = False,
) -> None:
    """
    Render a configuration error banner with consistent styling.

    This is a reusable component for displaying provider configuration errors
    across the application. Use this instead of creating custom error displays.

    Args:
        provider_name: Name of the misconfigured provider (e.g., "anthropic")
        config_errors: List of specific error messages
        show_back_button: Whether to show a "Back to Jobs" button
    """
    # Use a wrapper div with padding to ensure proper spacing on mobile
    # This avoids the w-full + margin overflow issue
    with ui.element("div").classes("w-full px-4 mt-4 box-border"):
        with ui.card().classes("w-full bg-red-50 border-l-4 border-red-500"):
            with ui.row().classes("items-start gap-3 flex-wrap"):
                ui.icon("error", color="red", size="md").classes("flex-shrink-0 mt-1")
                with ui.column().classes("gap-1 flex-1 min-w-0"):
                    ui.label("Server Configuration Error").classes(
                        "text-red-800 font-bold text-base sm:text-lg"
                    )
                    ui.label(
                        f"The {provider_name.upper()} provider is not configured correctly. "
                        "Jobs cannot be started until this is resolved."
                    ).classes("text-red-700 text-sm sm:text-base break-words")
                    ui.label("Please contact the system administrator.").classes(
                        "text-red-600 text-xs sm:text-sm"
                    )

            with ui.expansion("Technical Details", icon="info").classes("mt-2 w-full"):
                for error in config_errors:
                    ui.label(f"• {error}").classes(
                        "text-red-600 text-xs sm:text-sm font-mono break-words"
                    )

            if show_back_button:
                ui.button(
                    "Back to Jobs",
                    on_click=lambda: ui.navigate.to("/jobs"),
                    icon="arrow_back",
                ).classes("mt-4")


def render_alert_banner(
    title: str,
    message: str,
    severity: str = "error",
    details: list[str] | None = None,
    expansion_title: str = "Details",
) -> None:
    """
    Render a generic alert banner with consistent styling.

    This is a reusable component for displaying alerts (errors, warnings, info)
    across the application.

    Args:
        title: Alert title
        message: Main alert message
        severity: One of "error", "warning", "info" (affects colors)
        details: Optional list of detail messages shown in expansion
        expansion_title: Title for the expandable details section
    """
    # Color mappings
    colors = {
        "error": {
            "bg": "bg-red-50",
            "border": "border-red-500",
            "icon_color": "red",
            "title": "text-red-800",
            "message": "text-red-700",
            "detail": "text-red-600",
        },
        "warning": {
            "bg": "bg-yellow-50",
            "border": "border-yellow-500",
            "icon_color": "orange",
            "title": "text-yellow-800",
            "message": "text-yellow-700",
            "detail": "text-yellow-600",
        },
        "info": {
            "bg": "bg-blue-50",
            "border": "border-blue-500",
            "icon_color": "blue",
            "title": "text-blue-800",
            "message": "text-blue-700",
            "detail": "text-blue-600",
        },
    }
    c = colors.get(severity, colors["error"])
    icon_name = {"error": "error", "warning": "warning", "info": "info"}.get(severity, "error")

    # Use a wrapper div with padding to ensure proper spacing on mobile
    with ui.element("div").classes("w-full px-4 mt-4 box-border"):
        with ui.card().classes(f"w-full {c['bg']} border-l-4 {c['border']}"):
            with ui.row().classes("items-start gap-3 flex-wrap"):
                ui.icon(icon_name, color=c["icon_color"], size="md").classes("flex-shrink-0 mt-1")
                with ui.column().classes("gap-1 flex-1 min-w-0"):
                    ui.label(title).classes(f"{c['title']} font-bold text-base sm:text-lg")
                    ui.label(message).classes(f"{c['message']} text-sm sm:text-base break-words")

            if details:
                with ui.expansion(expansion_title, icon="info").classes("mt-2 w-full"):
                    for detail in details:
                        ui.label(f"• {detail}").classes(
                            f"{c['detail']} text-xs sm:text-sm font-mono break-words"
                        )


def get_status_badge_props(status: JobStatus) -> Dict:
    """
    Get NiceGUI badge properties for a job status.

    Args:
        status: JobStatus enum value

    Returns:
        Dict with keys: color, icon, text, classes
    """
    color = STATUS_COLORS.get(status, "gray")
    icon = STATUS_ICONS.get(status, "○")

    # Special styling for failed jobs
    classes = ""
    if status == JobStatus.FAILED:
        classes = "bg-red-600 text-white font-bold"

    return {"color": color, "icon": icon, "text": status.value, "classes": classes}


def render_status_cell_slot() -> str:
    """
    Generate Quasar table slot template for status column with enhanced failed job display.

    Returns slot template string with:
    - Colored badges based on status
    - Icons for each status type
    - Red background with white text for failed jobs
    - Tooltip showing error preview on hover for failed jobs

    Returns:
        Quasar slot template string
    """
    return r"""
        <q-td :props="props">
            <div class="row items-center gap-2">
                <!-- Failed status: Red badge with white text and error tooltip -->
                <q-badge
                    v-if="props.row.status === 'failed'"
                    color="red"
                    text-color="white"
                    class="px-3 py-1 font-bold cursor-pointer"
                >
                    <div class="row items-center gap-1">
                        <span>✗</span>
                        <span>{{ props.row.status }}</span>
                    </div>
                    <q-tooltip
                        v-if="props.row.error"
                        class="bg-red-800 text-white text-sm"
                        max-width="400px"
                        anchor="top middle"
                        self="bottom middle"
                    >
                        <div class="font-bold mb-1">Error:</div>
                        <div>{{ props.row.error.substring(0, 150) }}{{ props.row.error.length > 150 ? '...' : '' }}</div>
                        <div class="text-xs mt-2 italic">Click "View" for details</div>
                    </q-tooltip>
                </q-badge>

                <!-- Completed status: Green badge -->
                <q-badge
                    v-else-if="props.row.status === 'completed'"
                    color="green"
                    class="px-2 py-1"
                >
                    <div class="row items-center gap-1">
                        <span>✓</span>
                        <span>{{ props.row.status }}</span>
                    </div>
                </q-badge>

                <!-- Running status: Yellow badge with animation -->
                <q-badge
                    v-else-if="props.row.status === 'running'"
                    color="yellow"
                    text-color="black"
                    class="px-2 py-1"
                >
                    <div class="row items-center gap-1">
                        <span>▶</span>
                        <span>{{ props.row.status }}</span>
                    </div>
                </q-badge>

                <!-- Queued status: Blue badge -->
                <q-badge
                    v-else-if="props.row.status === 'queued'"
                    color="blue"
                    class="px-2 py-1"
                >
                    <div class="row items-center gap-1">
                        <span>⟳</span>
                        <span>{{ props.row.status }}</span>
                    </div>
                </q-badge>

                <!-- Awaiting feedback: Orange badge -->
                <q-badge
                    v-else-if="props.row.status === 'awaiting_feedback'"
                    color="orange"
                    class="px-2 py-1"
                >
                    <div class="row items-center gap-1">
                        <span>⏸</span>
                        <span>{{ props.row.status }}</span>
                    </div>
                </q-badge>

                <!-- Cancelled status: Gray badge -->
                <q-badge
                    v-else-if="props.row.status === 'cancelled'"
                    color="grey"
                    class="px-2 py-1"
                >
                    <div class="row items-center gap-1">
                        <span>⊗</span>
                        <span>{{ props.row.status }}</span>
                    </div>
                </q-badge>

                <!-- Default: Gray badge -->
                <q-badge
                    v-else
                    color="grey"
                    class="px-2 py-1"
                >
                    <div class="row items-center gap-1">
                        <span>○</span>
                        <span>{{ props.row.status }}</span>
                    </div>
                </q-badge>
            </div>
        </q-td>
    """


# View button slot template for job tables
VIEW_BUTTON_SLOT = r"""
<q-td :props="props">
    <q-btn flat dense color="primary" label="View"
           @click="$parent.$emit('view-job', props.row.job_id)" />
</q-td>
"""


def render_actions_slot_with_delete() -> str:
    """
    Generate Quasar table slot template for actions column with view, share, and delete buttons.

    Returns slot template string with:
    - View icon button (always visible) - uses visibility icon
    - Share icon button (conditionally shown via v-if="props.row.can_share") - uses share icon
    - Delete icon button (conditionally shown via v-if="props.row.can_delete") - uses delete icon
    - All buttons use round style for a compact, badge-like appearance
    - Tooltips for clarity

    Returns:
        Quasar slot template string
    """
    return r"""
        <q-td :props="props">
            <div class="row items-center gap-1 justify-center">
                <!-- View button - always visible -->
                <q-btn
                    round
                    flat
                    dense
                    size="sm"
                    color="primary"
                    icon="visibility"
                    @click="$parent.$emit('view-job', props.row.job_id)"
                >
                    <q-tooltip>View job details</q-tooltip>
                </q-btn>

                <!-- Share button - conditionally shown based on can_share (owners only) -->
                <q-btn
                    v-if="props.row.can_share"
                    round
                    flat
                    dense
                    size="sm"
                    color="secondary"
                    icon="share"
                    @click="$parent.$emit('share-job', props.row.job_id)"
                >
                    <q-tooltip>Share job</q-tooltip>
                </q-btn>

                <!-- Delete button - conditionally shown based on can_delete -->
                <q-btn
                    v-if="props.row.can_delete"
                    round
                    flat
                    dense
                    size="sm"
                    color="negative"
                    icon="delete"
                    @click="$parent.$emit('delete-job', props.row.job_id)"
                >
                    <q-tooltip>Delete job</q-tooltip>
                </q-btn>
            </div>
        </q-td>
    """


def render_job_action_buttons(
    on_share: Callable[[], None] | None = None,
    on_delete: Callable[[], None] | None = None,
) -> None:
    """
    Render job action buttons (share, delete) in the same style as table actions.

    Uses round, flat, dense icon buttons with tooltips - same visual style as
    the table action column buttons from render_actions_slot_with_delete().

    Args:
        on_share: Callback for share button click. If None, share button is hidden.
        on_delete: Callback for delete button click. If None, delete button is hidden.
    """
    with ui.row().classes("gap-1 items-center"):
        if on_share:
            with ui.button(icon="share", on_click=on_share).props(
                "round flat dense size=sm color=secondary"
            ):
                ui.tooltip("Share job")

        if on_delete:
            with ui.button(icon="delete", on_click=on_delete).props(
                "round flat dense size=sm color=negative"
            ):
                ui.tooltip("Delete job")


def render_navigator(
    active_page: str | None = None,
    show_new_job: bool = True,
    extra_buttons: list[tuple[str, str, Callable[[], None], str]] | None = None,
) -> None:
    """
    Render the standard navigation header for all authenticated pages.

    Provides consistent navigation across the application with links to
    New Job, Billing, Docs, and Admin pages. The SHANDY logo/title acts
    as a home button linking to the jobs list.

    On mobile screens (< 640px), navigation buttons are collapsed into a
    hamburger menu that opens a right-side drawer.

    Args:
        active_page: Current page name for highlighting ("jobs", "new", "billing", "docs", "admin")
        show_new_job: Whether to show the New Job button (disable on config error)
        extra_buttons: List of (label, icon, on_click, props) tuples for page-specific buttons
    """
    from nicegui import app

    # Add mobile icon meta tags to page head
    ui.add_head_html(
        '<link rel="apple-touch-icon" sizes="180x180" href="/assets/apple-touch-icon.png">'
    )
    ui.add_head_html('<link rel="manifest" href="/assets/manifest.json">')
    ui.add_head_html('<meta name="theme-color" content="#0891b2">')

    # Check admin status from session storage (set by require_auth decorator)
    show_admin = app.storage.user.get("is_admin", False)
    # Add responsive CSS for mobile/desktop navigation toggle
    ui.add_css(
        """
        @media (max-width: 639px) {
            .mobile-menu-btn { display: inline-flex !important; }
            .desktop-nav { display: none !important; }
        }
        @media (min-width: 640px) {
            .mobile-menu-btn { display: none !important; }
            .desktop-nav { display: flex !important; }
        }
    """
    )

    # Button styles: active = white bg with cyan text, inactive = flat white
    active_style = "unelevated color=white text-color=primary"
    inactive_style = "flat color=white"

    # Define navigation items for reuse in both desktop and mobile views
    nav_items: list[tuple[str, str, str, bool]] = []
    if show_new_job:
        nav_items.append(("New", "add", "/new", active_page == "new"))
    nav_items.extend(
        [
            ("Billing", "payments", "/billing", active_page == "billing"),
            ("Docs", "description", "/docs", active_page == "docs"),
        ]
    )
    if show_admin:
        nav_items.append(("Admin", "admin_panel_settings", "/admin", active_page == "admin"))

    # Mobile drawer for navigation
    with ui.right_drawer(value=False).props("overlay behavior=mobile bordered") as drawer:
        drawer.classes("bg-primary")
        with ui.column().classes("w-full gap-2 p-4"):
            ui.label("Navigation").classes("text-white text-h6 font-bold mb-2")

            # Page-specific extra buttons in drawer
            if extra_buttons:
                for label, icon, on_click, props in extra_buttons:

                    def make_drawer_click(fn: Callable[[], None]) -> Callable[[], None]:
                        def handler() -> None:
                            drawer.set_value(False)
                            fn()

                        return handler

                    ui.button(
                        label,
                        on_click=make_drawer_click(on_click),
                        icon=icon,
                    ).props("flat color=white align=left").classes("w-full justify-start")

            # Navigation items in drawer
            for label, icon, route, is_active in nav_items:
                style = active_style if is_active else "flat color=white"

                def make_nav_click(r: str) -> Callable[[], None]:
                    def handler() -> None:
                        drawer.set_value(False)
                        ui.navigate.to(r)

                    return handler

                ui.button(
                    label,
                    on_click=make_nav_click(route),
                    icon=icon,
                ).props(f"{style} align=left").classes("w-full justify-start")

            ui.separator().classes("bg-white/30 my-2")

            # Logout button in drawer
            ui.button(
                "Logout",
                on_click=lambda: ui.navigate.to("/auth/logout"),
                icon="logout",
            ).props("flat color=white align=left").classes("w-full justify-start")

    with ui.header().classes("items-center justify-between"):
        # Title section - clickable to go home
        with ui.link(target="/jobs").classes("no-underline"):
            with ui.row().classes("items-center gap-2 cursor-pointer"):
                # Logo in white circle
                with ui.element("div").classes(
                    "w-10 h-10 rounded-full bg-white flex items-center justify-center"
                ):
                    ui.image("/assets/logo.svg").classes("w-8 h-8")
                # SHANDY text in white
                ui.label("SHANDY").classes("text-white text-h5 font-bold")

        # Mobile hamburger menu button (visible on small screens only)
        hamburger = ui.button(
            icon="menu",
            on_click=lambda: drawer.set_value(True),
        ).props("flat color=white")
        hamburger.style("display: none").classes("mobile-menu-btn")

        # Desktop navigation section (hidden on small screens)
        nav_row = ui.row().classes("gap-1 desktop-nav")
        nav_row.style("display: flex")

        with nav_row:
            # Page-specific extra buttons first
            if extra_buttons:
                for label, icon, on_click, props in extra_buttons:
                    btn = ui.button(label, on_click=on_click, icon=icon)
                    if props:
                        btn.props(props)

            # Standard navigation buttons
            for label, icon, route, is_active in nav_items:
                style = active_style if is_active else inactive_style
                ui.button(
                    label,
                    on_click=lambda r=route: ui.navigate.to(r),
                    icon=icon,
                ).props(style)

            # Logout button
            ui.button(
                "Logout",
                on_click=lambda: ui.navigate.to("/auth/logout"),
                icon="logout",
            ).props(inactive_style)


def render_stat_badges(
    stats: list[tuple[str, str | int, str]],
    icon_map: dict[str, str] | None = None,
) -> None:
    """
    Render a compact row of stat badges - mobile-friendly inline display.

    Creates a responsive row of badges with icons, labels, and values.
    Wraps gracefully on mobile screens.

    Args:
        stats: List of (label, value, color) tuples where color is a Quasar color name
               (e.g., "blue", "green", "red") or empty string for default gray
        icon_map: Optional mapping of label to Material icon name

    Example:
        render_stat_badges([
            ("Total", 42, ""),
            ("Running", 3, "blue"),
            ("Completed", 39, "green"),
        ])
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
    }
    icons = {**default_icons, **(icon_map or {})}

    with ui.row().classes("w-full gap-2 flex-wrap items-center mb-2"):
        for label, value, color in stats:
            badge_color = color if color else "gray"
            icon = icons.get(label, "tag")
            with ui.badge(color=badge_color).props("outline").classes("px-3 py-1 text-sm"):
                with ui.row().classes("items-center gap-1"):
                    ui.icon(icon, size="xs")
                    ui.label(f"{label}: {value}").classes("font-medium")


def render_empty_state(message: str) -> None:
    """
    Render a styled empty state message.

    Args:
        message: The message to display
    """
    ui.label(message).classes("text-gray-500 text-center p-8")


def render_dialog_actions(
    on_confirm: Callable[[], None | Awaitable[None]],
    on_cancel: Callable[[], None],
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    confirm_props: str = "color=primary",
) -> None:
    """
    Render standard dialog action buttons (Cancel/Confirm).

    Creates a right-aligned row with Cancel and Confirm buttons.
    Use this in dialogs to ensure consistent footer styling.

    Args:
        on_confirm: Callback when confirm button is clicked (can be async)
        on_cancel: Callback when cancel button is clicked
        confirm_label: Text for the confirm button
        cancel_label: Text for the cancel button
        confirm_props: Quasar props for the confirm button
    """
    with ui.row().classes("w-full justify-end gap-2 mt-4"):
        ui.button(cancel_label, on_click=on_cancel).props("flat")
        ui.button(confirm_label, on_click=on_confirm).props(confirm_props)


def make_action_button_slot(
    label: str,
    event_name: str,
    icon: str | None = None,
    color: str = "primary",
    row_id_field: str = "job_id",
) -> str:
    """
    Generate a Quasar table slot template for an action button.

    Creates an HTML template string for use with NiceGUI's table.add_slot().
    The button emits an event with the row data when clicked.

    Args:
        label: Button label text
        event_name: Event name emitted when button is clicked
        icon: Optional Material icon name (e.g., "person_add")
        color: Quasar color for the button
        row_id_field: Field name in row data to use for identification

    Returns:
        Quasar slot template string

    Example:
        table.add_slot("body-cell-actions", make_action_button_slot(
            label="Assign",
            event_name="assign",
            icon="person_add",
        ))
        table.on("assign", handle_assign)
    """
    icon_attr = f'icon="{icon}"' if icon else ""
    return f"""
<q-td :props="props">
    <q-btn
        size="sm"
        color="{color}"
        {icon_attr}
        label="{label}"
        @click="$parent.$emit('{event_name}', props.row)"
    />
</q-td>
"""


async def render_user_search(
    on_select: AsyncCallback,
    placeholder: str = "Search by email or name",
    action_label: str = "Select",
    action_icon: str = "check",
) -> tuple[ui.input, ui.column]:
    """
    Render a user search input with results list.

    Creates a search input that queries users by email/name and displays
    results with action buttons. Used for share dialogs and admin assignment.

    Args:
        on_select: Async callback when a user is selected, receives user dict
                   with keys: id, name, email
        placeholder: Placeholder text for the search input
        action_label: Label for the select button
        action_icon: Icon for the select button

    Returns:
        Tuple of (search_input, results_container) for external reference
    """
    from sqlalchemy import select

    from shandy.database.models import User
    from shandy.database.session import get_admin_session

    search_input = ui.input(
        label=placeholder,
        placeholder="Type to search...",
    ).classes("w-full mb-4")

    results_container = ui.column().classes("w-full max-h-48 overflow-y-auto")

    async def search_users():
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

                            async def select_user(u=user):
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

        except Exception:
            results_container.clear()
            with results_container:
                ui.label("Search failed").classes("text-red-500 text-sm")

    search_input.on("input", search_users)

    return search_input, results_container


def render_share_dialog(job_id: str) -> ui.dialog:
    """
    Create and return a share dialog for a job.

    This is a reusable component for sharing jobs with other users.
    The dialog includes:
    - List of current shares with revoke buttons
    - User search to find users by email/name
    - Permission level selector (view/edit)

    Args:
        job_id: The job ID to share

    Returns:
        The dialog element (call .open() to show it)

    Example:
        share_dialog = render_share_dialog(job_id)
        ui.button("Share", on_click=share_dialog.open)
    """
    import logging

    from shandy.webapp_components.utils.http_client import api_delete, api_get, api_post

    logger = logging.getLogger(__name__)

    with ui.dialog() as dialog, ui.card().classes("w-[600px]"):
        ui.label("Share Job").classes("text-h6 mb-4")

        # Container for current shares
        shares_container = ui.column().classes("w-full mb-4")

        async def refresh_shares():
            """Load and display current shares."""
            shares_container.clear()

            try:
                response = await api_get(f"/web/shares/job/{job_id}")

                if response.status_code == 200:
                    shares = response.json()

                    if shares:
                        with shares_container:
                            ui.label("Current Shares").classes("text-subtitle2 font-bold mb-2")
                            for share in shares:
                                with ui.card().classes("w-full p-2"):
                                    with ui.row().classes("items-center justify-between w-full"):
                                        with ui.column():
                                            ui.label(share["shared_with_name"]).classes("font-bold")
                                            ui.label(share["shared_with_email"]).classes(
                                                "text-sm text-gray-600"
                                            )
                                        with ui.row().classes("items-center gap-2"):
                                            ui.badge(
                                                share["permission_level"],
                                                color="blue",
                                            )
                                            ui.button(
                                                icon="delete",
                                                on_click=lambda s=share: revoke_share(s["id"]),
                                            ).props("flat dense color=red")
                    else:
                        with shares_container:
                            ui.label("No shares yet").classes("text-gray-500 italic")
                elif response.status_code == 403:
                    with shares_container:
                        ui.label("You can only view shares for jobs you own").classes(
                            "text-red-600"
                        )
            except Exception as e:
                logger.error("Failed to load shares: %s", e)
                with shares_container:
                    ui.label("Failed to load shares").classes("text-red-600")

        async def revoke_share(share_id: str):
            """Revoke a job share."""
            try:
                response = await api_delete(f"/web/shares/{share_id}")

                if response.status_code == 200:
                    ui.notify("Share revoked successfully", type="positive")
                    await refresh_shares()
                else:
                    ui.notify("Failed to revoke share", type="negative")
            except Exception as e:
                logger.error("Failed to revoke share: %s", e)
                ui.notify("Error revoking share", type="negative")

        ui.separator()

        # Add new share section
        ui.label("Add New Share").classes("text-subtitle2 font-bold mb-2")

        # User search
        search_input = ui.input(
            "Search by email or name",
            placeholder="user@example.com",
        ).classes("w-full")

        # Search results container
        search_results = ui.column().classes("w-full mb-4")

        # Permission level selector
        permission_select = ui.select(
            ["view", "edit"],
            value="view",
            label="Permission Level",
        ).classes("w-full")

        async def search_users(search_query: str):
            """Search for users by email or name."""
            search_results.clear()

            if not search_query or len(search_query) < 2:
                return

            try:
                response = await api_get(f"/web/shares/search/users?q={search_query}")

                if response.status_code == 200:
                    users = response.json()

                    if users:
                        with search_results:
                            ui.label(f"Found {len(users)} user(s)").classes(
                                "text-sm text-gray-600 mb-2"
                            )
                            for user in users:
                                with ui.card().classes(
                                    "w-full p-2 cursor-pointer hover:bg-gray-100"
                                ):
                                    with (
                                        ui.row()
                                        .classes("items-center justify-between w-full")
                                        .on(
                                            "click",
                                            lambda u=user: share_with_user(u["email"]),
                                        )
                                    ):
                                        with ui.column():
                                            ui.label(user["name"]).classes("font-bold")
                                            ui.label(user["email"]).classes("text-sm text-gray-600")
                                        ui.button(icon="person_add").props(
                                            "flat dense color=primary"
                                        )
                    else:
                        with search_results:
                            ui.label("No users found").classes("text-gray-500 italic")
            except Exception as e:
                logger.error("Failed to search users: %s", e)
                with search_results:
                    ui.label("Search failed").classes("text-red-600")

        async def share_with_user(email: str):
            """Share job with a user."""
            try:
                response = await api_post(
                    f"/web/shares/job/{job_id}",
                    json={
                        "shared_with_email": email,
                        "permission_level": permission_select.value,
                    },
                )

                if response.status_code == 200:
                    ui.notify(f"Shared with {email}", type="positive")
                    search_input.value = ""
                    search_results.clear()
                    await refresh_shares()
                elif response.status_code == 400:
                    error = response.json()
                    ui.notify(error.get("detail", "Failed to share"), type="warning")
                else:
                    ui.notify("Failed to share job", type="negative")
            except Exception as e:
                logger.error("Failed to share job: %s", e)
                ui.notify("Error sharing job", type="negative")

        # Bind search input to trigger search
        search_input.on("input", lambda e: search_users(e.value))

        # Dialog actions
        with ui.row().classes("w-full justify-end gap-2 mt-4"):
            ui.button("Close", on_click=dialog.close)

        # Load shares when dialog opens
        dialog.on("open", lambda: refresh_shares())

    return dialog


def render_delete_dialog(
    job_id: str,
    job_manager: "JobManager",  # type: ignore[name-defined]  # noqa: F821
    on_deleted: Callable[[], None] | None = None,
) -> ui.dialog:
    """
    Create and return a delete confirmation dialog for a job.

    This is a reusable component for deleting jobs with confirmation.

    Args:
        job_id: The job ID to delete
        job_manager: The job manager instance
        on_deleted: Optional callback to run after successful deletion

    Returns:
        The dialog element (call .open() to show it)

    Example:
        delete_dialog = render_delete_dialog(job_id, job_manager, on_deleted=refresh_table)
        ui.button("Delete", on_click=delete_dialog.open)
    """
    import logging

    logger = logging.getLogger(__name__)

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Delete Job").classes("text-h6 font-bold")
        ui.label(f"Are you sure you want to delete job {job_id}?").classes("text-body1 my-2")
        ui.label(
            "This action cannot be undone. All job data and findings will be permanently deleted."
        ).classes("text-caption text-red-600")

        async def on_confirm():
            dialog.close()
            try:
                job_manager.delete_job(job_id)
                ui.notify(f"Job {job_id} deleted successfully", type="positive")
                if on_deleted:
                    result = on_deleted()
                    # Support async callbacks
                    if hasattr(result, "__await__"):
                        await result
            except ValueError as e:
                ui.notify(str(e), type="negative")
            except Exception as e:
                logger.error("Failed to delete job %s: %s", job_id, e)
                ui.notify(f"Failed to delete job: {e}", type="negative")

        render_dialog_actions(
            on_confirm=on_confirm,
            on_cancel=dialog.close,
            confirm_label="Delete",
            confirm_props="color=negative",
        )

    return dialog
