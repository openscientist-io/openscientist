"""
Reusable UI components for SHANDY web interface.

Provides UI rendering functions for error displays, status badges,
page headers, and other common interface elements.
"""

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
            ("Admin", "admin_panel_settings", "/admin", active_page == "admin"),
        ]
    )

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
        with ui.row().classes("items-center gap-2"):
            ui.button(
                "SHANDY",
                on_click=lambda: ui.navigate.to("/jobs"),
                icon="home",
            ).props("unelevated color=white text-color=primary").classes("text-h5 font-bold")

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


def render_stat_card(
    label: str,
    value: str | int,
    color_class: str = "",
) -> None:
    """
    Render a single stat card.

    Args:
        label: Label text shown above the value
        value: The stat value (string or int)
        color_class: Optional Tailwind color class for the value (e.g., "text-blue-600")
    """
    with ui.card():
        ui.label(label).classes("text-subtitle2")
        value_classes = "text-h4"
        if color_class:
            value_classes = f"{value_classes} {color_class}"
        ui.label(str(value)).classes(value_classes)


def render_stat_row(
    stats: list[tuple[str, str | int, str]],
) -> None:
    """
    Render a row of stat cards.

    Args:
        stats: List of (label, value, color_class) tuples
    """
    with ui.row().classes("w-full gap-4 p-4"):
        for label, value, color_class in stats:
            render_stat_card(label, value, color_class)


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


def render_metric_card(
    label: str,
    value: str | int,
    color_class: str = "",
    progress: float | None = None,
    badge_color: str | None = None,
    subtitle: str | None = None,
) -> None:
    """
    Render a metric card with optional progress bar and badge.

    More feature-rich than render_stat_card() - supports progress bars,
    colored badges, and subtitles for displaying job status metrics.

    Args:
        label: Label text shown above the value
        value: The metric value (string or int)
        color_class: Optional Tailwind color class for the value (e.g., "text-blue-600")
        progress: Optional progress value (0.0 to 1.0) to show a linear progress bar
        badge_color: If set, renders value as a colored badge instead of plain text
        subtitle: Optional subtitle shown below the value
    """
    with ui.card().classes("flex-1"):
        ui.label(label).classes("text-subtitle2")
        if badge_color:
            ui.badge(str(value), color=badge_color).classes("text-h6")
        else:
            value_classes = "text-h5"
            if color_class:
                value_classes = f"{value_classes} {color_class}"
            ui.label(str(value)).classes(value_classes)
        if progress is not None:
            ui.linear_progress(progress)
        if subtitle:
            ui.label(subtitle).classes("text-sm text-gray-600")


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
    from shandy.database.rls import bypass_rls
    from shandy.database.session import get_session

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
            async with get_session() as session:
                async with bypass_rls(session):
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
