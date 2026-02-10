"""
Reusable UI components for SHANDY web interface.

Provides UI rendering functions for error displays, status badges,
and other common interface elements.
"""

from pathlib import Path
from typing import Dict

from nicegui import ui

from shandy.job_manager import JobInfo, JobStatus

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
                        ui.run_javascript(f"""
                            navigator.clipboard.writeText({repr(error_info["raw"])});
                        """)
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
