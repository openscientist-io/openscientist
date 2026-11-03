"""Error and alert banners for the web app."""

import html
from pathlib import Path
from typing import Any

from nicegui import ui

from openscientist.job.types import JobInfo
from openscientist.job_files import job_file_exists


def render_justified_text(
    text: str,
    text_classes: str = "text-sm text-gray-700",
) -> None:
    """Render text as a justified paragraph with automatic hyphenation."""
    if not text:
        return

    ui.html(
        f'<p class="{text_classes}" style="text-align:justify;hyphens:auto;'
        f'text-justify:inter-word;margin:0;">{html.escape(text)}</p>'
    )


def render_error_card(error_info: dict[str, Any], job_info: JobInfo, job_dir: Path) -> None:
    """Render a user-friendly error card with tiered disclosure.

    ``error_info`` is the dict from ``get_user_friendly_error()`` (category, title,
    message, extracted_error, steps, raw, contact_admin).
    """
    with ui.card().classes("w-full bg-red-50 border-2 border-red-300 mb-4 p-6"):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("error", size="lg").classes("text-red-600")
            ui.label(error_info["title"]).classes("text-h6 font-bold text-red-800")

        ui.label(error_info["message"]).classes("text-red-700 mb-4")

        if error_info.get("steps"):
            ui.label("How to resolve:").classes("font-bold text-red-800 mb-2")
            with ui.column().classes("gap-2 mb-4 pl-2"):
                for step in error_info["steps"]:
                    with ui.row().classes("gap-2 items-start"):
                        ui.label("•").classes("text-red-600 font-bold")
                        ui.label(step).classes("text-sm text-red-700")

        if error_info.get("contact_admin"):
            ui.label("This error requires administrator assistance.").classes(
                "text-sm text-red-600 italic mt-2 mb-3 px-2 py-2 bg-red-100 rounded"
            )

        with (
            ui.expansion("What happened?", icon="info").classes(
                "w-full mt-3 border border-red-200 rounded"
            ),
            ui.column().classes("gap-2 p-3"),
        ):
            ui.label(f"Error Category: {error_info['category'].title()}").classes(
                "text-sm font-bold text-gray-800"
            )
            ui.label(f"Job Status: {job_info.status.value}").classes("text-sm text-gray-700")
            ui.label(
                f"Iterations Completed: {job_info.iterations_completed}/{job_info.max_iterations}"
            ).classes("text-sm text-gray-700")
            if job_info.failed_at:
                ui.label(f"Failed At: {job_info.failed_at[:19]}").classes("text-sm text-gray-700")

            if error_info.get("extracted_error"):
                ui.label("Error Message:").classes("text-sm font-bold mt-3 text-gray-800")
                with ui.element("div").classes("w-full overflow-hidden"):
                    ui.label(error_info["extracted_error"]).classes(
                        "text-sm bg-white p-3 rounded border border-red-200 text-gray-700"
                    ).style("word-break: break-word; overflow-wrap: break-word;")

        with (
            ui.expansion("Technical Details", icon="code").classes(
                "w-full mt-2 border border-red-200 rounded overflow-hidden"
            ),
            ui.column().classes("gap-2 p-3 w-full"),
        ):
            with ui.row().classes("items-center justify-between w-full mb-2 flex-nowrap"):
                ui.label("Raw Error Output:").classes("text-sm font-bold text-gray-800")

                def copy_to_clipboard() -> None:
                    ui.run_javascript(
                        f"""
                            navigator.clipboard.writeText({error_info["raw"]!r});
                        """
                    )
                    ui.notify("Error message copied to clipboard", type="positive")

                with (
                    ui.button(icon="content_copy", on_click=copy_to_clipboard)
                    .props("flat dense color=primary size=sm")
                    .classes("flex-shrink-0")
                ):
                    ui.tooltip("Copy to clipboard")

            with ui.element("div").classes("w-full overflow-x-auto"):
                ui.code(error_info["raw"], language="text").classes(
                    "text-xs max-h-[300px] overflow-y-auto p-2 bg-gray-50 rounded"
                ).style("word-break: break-word; white-space: pre-wrap; max-width: 100%;")

            # The job directory is agent-writable, so the hint only shows when
            # a real (not a linked) log file is there.
            if job_file_exists(job_dir, "orchestrator.log"):
                ui.label("Check the orchestrator.log file for complete details.").classes(
                    "text-xs text-gray-600 mt-2"
                )


def render_config_error_banner(
    provider_name: str,
    config_errors: list[str],
    show_back_button: bool = False,
) -> None:
    """Render a configuration error banner for a misconfigured provider."""
    # Wrapper div with padding avoids the w-full + margin overflow on mobile
    with (
        ui.element("div").classes("w-full px-4 mt-4 box-border"),
        ui.card().classes("w-full bg-red-50 border-l-4 border-red-500"),
    ):
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
    """Render a generic alert banner for error, warning or info severity."""
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

    # Wrapper div with padding avoids the w-full + margin overflow on mobile
    with (
        ui.element("div").classes("w-full px-4 mt-4 box-border"),
        ui.card().classes(f"w-full {c['bg']} border-l-4 {c['border']}"),
    ):
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


def render_pending_approval_notice() -> None:
    """Render an informational notice for users awaiting administrator approval."""
    with (
        ui.card().classes("w-full border-l-4 border-amber-500 bg-amber-50"),
        ui.row().classes("items-start gap-3"),
    ):
        ui.icon("hourglass_top", color="amber", size="md")
        with ui.column().classes("gap-1"):
            ui.label("Account Pending Approval").classes("text-amber-900 font-bold")
            ui.label(
                "Your account is waiting for administrator approval. "
                "You can browse existing pages, but starting new jobs "
                "is disabled until approval."
            ).classes("text-amber-800")
