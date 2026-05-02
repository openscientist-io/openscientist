"""New job submission page."""

import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from nicegui import ui

from openscientist.auth import can_current_user_start_jobs, get_current_user_id, require_auth
from openscientist.job_templates import (
    FREEFORM_TEMPLATE_ID,
    get_job_template,
    get_job_template_options,
    merge_template_prompt,
)
from openscientist.providers import check_provider_config
from openscientist.webapp_components.ui_components import (
    render_config_error_banner,
    render_job_template_guidance,
    render_navigator,
    render_pending_approval_notice,
)
from openscientist.webapp_components.utils.session import (
    add_uploaded_file,
    clear_uploaded_files,
    get_uploaded_files,
)

logger = logging.getLogger(__name__)


def _build_upload_session_id(user_id: str | None, client: object) -> str:
    """Build an upload-session key scoped to user and websocket client."""
    effective_user_id = user_id or "anonymous"
    client_id = str(getattr(client, "id", id(client)))
    return f"{effective_user_id}:{client_id}"


def _persist_uploaded_files(session_id: str) -> list[Path]:
    """Return temp file paths for all uploaded files in the session."""
    return [uploaded_file["path"] for uploaded_file in get_uploaded_files(session_id)]


def _notify_creation_error(error: Exception) -> None:
    """Show user-friendly notification for job creation failures."""
    error_msg = str(error).lower()
    if "authentication" in error_msg or "api key" in error_msg:
        ui.notify(
            "Authentication error. Please contact your administrator to check API credentials.",
            type="negative",
        )
        return
    if "event loop" in error_msg:
        ui.notify("Internal server error. Please try again or contact support.", type="negative")
        return
    ui.notify("Error creating job. Please try again or contact support.", type="negative")


def _apply_selected_template_to_prompt(current_prompt: str | None, template_id: str | None) -> str:
    """Return prompt text after applying the selected editable job template."""
    template = get_job_template(template_id)
    if template is None:
        return current_prompt or ""
    return merge_template_prompt(current_prompt, template)


def _submit_job(
    *,
    job_manager: Any,
    user_can_start_jobs: bool,
    session_id: str,
    research_question: ui.textarea,
    max_iterations: ui.number,
    use_hypotheses: ui.switch,
    coinvestigate_mode: ui.switch,
) -> None:
    """Validate input and create a new discovery job."""
    if not user_can_start_jobs:
        ui.notify("Your account is pending administrator approval.", type="warning")
        return

    question = research_question.value.strip()
    if not question:
        ui.notify("Please enter a research question", type="negative")
        return

    current_user_id = get_current_user_id()
    if not current_user_id:
        ui.notify("Authentication required. Please log in again.", type="negative")
        ui.navigate.to("/login")
        return

    job_id = str(uuid.uuid4())
    data_files = _persist_uploaded_files(session_id)
    mode = "coinvestigate" if coinvestigate_mode.value else "autonomous"

    try:
        job_manager.create_job(
            job_id=job_id,
            research_question=question,
            data_files=data_files,
            max_iterations=int(max_iterations.value),
            use_hypotheses=use_hypotheses.value,
            auto_start=True,
            investigation_mode=mode,
            owner_id=current_user_id,
        )
        ui.notify(f"Job {job_id} created and started!", type="positive")
        clear_uploaded_files(session_id)
        ui.navigate.to(f"/job/{job_id}")
    except Exception as exc:
        logger.error("Error creating job: %s", exc, exc_info=True)
        _notify_creation_error(exc)


async def _handle_upload(e: Any, session_id: str) -> None:
    """Stream upload directly to a temp file and record its path in session state."""
    try:
        name = e.file.name
        temp_path = Path(tempfile.mkdtemp()) / name
        await e.file.save(temp_path)
        add_uploaded_file(session_id, name, temp_path)
        ui.notify(f"Uploaded: {name}", type="positive")
        logger.info("Successfully uploaded %s (%d bytes)", name, e.file.size())
    except (ValueError, OSError) as exc:
        logger.error("Upload failed: %s", exc, exc_info=True)
        ui.notify(f"Upload failed: {exc}", type="negative")


@ui.page("/new")
@require_auth
def new_job_page() -> None:
    """Job submission form."""
    from openscientist import web_app

    job_manager = web_app.get_job_manager()
    user_can_start_jobs = can_current_user_start_jobs()
    user_id = get_current_user_id()
    client = ui.context.client
    session_id = _build_upload_session_id(user_id, client)
    client.on_disconnect(lambda: clear_uploaded_files(session_id))

    is_configured, provider_name, config_errors = check_provider_config()
    render_navigator(active_page="new", show_new_job=is_configured)

    if not user_can_start_jobs:
        render_pending_approval_notice()
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs")).props(
            "outline color=primary"
        ).classes("mt-4")
        return

    if not is_configured:
        render_config_error_banner(provider_name, config_errors, show_back_button=True)
        return

    async def on_upload(event: Any) -> None:
        await _handle_upload(event, session_id)

    with ui.card().classes("w-full max-w-2xl mx-auto mt-8"):
        ui.label("Submit Discovery Job").classes("text-h5 mb-4")

        with ui.row().classes("w-full gap-3 items-end flex-wrap"):
            template_select = ui.select(
                options=get_job_template_options(),
                label="Template",
                value=FREEFORM_TEMPLATE_ID,
            ).classes("flex-grow min-w-64")
            template_select.props("outlined dense")
            insert_template_button = ui.button("Insert Template", icon="content_paste").props(
                "outline color=primary"
            )

        template_guidance = ui.column().classes("w-full gap-2")

        research_question = ui.textarea(
            label="Research Question",
            placeholder="e.g., What metabolic pathways are affected by hypothermia?",
            validation={"Too short": lambda value: len(value) >= 10},
        ).classes("w-full")

        template_state: dict[str, str] = {"last_prompt": ""}

        def refresh_template_guidance(template_id: str | None) -> None:
            """Refresh the selected template guidance panel."""
            template_guidance.clear()
            with template_guidance:
                render_job_template_guidance(get_job_template(template_id))

        def selected_template_id() -> str:
            """Return the currently selected template ID."""
            return str(template_select.value or FREEFORM_TEMPLATE_ID)

        def on_template_change(event: Any) -> None:
            """Prefill an empty prompt when a template is selected."""
            template_id = str(event.value or FREEFORM_TEMPLATE_ID)
            template = get_job_template(template_id)
            current_prompt = str(research_question.value or "")
            last_prompt = template_state["last_prompt"].strip()
            should_prefill = not current_prompt.strip() or current_prompt.strip() == last_prompt

            if template is not None and should_prefill:
                research_question.value = template.prompt.strip()
                template_state["last_prompt"] = template.prompt.strip()
            elif template is None:
                template_state["last_prompt"] = ""

            refresh_template_guidance(template_id)

        def insert_selected_template() -> None:
            """Insert the selected template into the editable prompt."""
            template_id = selected_template_id()
            template = get_job_template(template_id)
            if template is None:
                ui.notify("Choose a template to insert.", type="warning")
                return
            research_question.value = _apply_selected_template_to_prompt(
                str(research_question.value or ""),
                template_id,
            )
            template_state["last_prompt"] = template.prompt.strip()

        template_select.on_value_change(on_template_change)
        insert_template_button.on("click", insert_selected_template)

        ui.upload(
            label="Upload Data Files (Optional - Tabular, Structures, Sequences, Images)",
            multiple=True,
            auto_upload=True,
            on_upload=on_upload,
        ).classes("w-full")
        ui.label("Maximum file size: 500 MB per file").classes("text-caption text-grey-6")

        max_iterations = ui.number(
            label="Max Iterations",
            value=10,
            min=2,
            max=100,
            step=1,
        ).classes("w-full")

        ui.separator().classes("my-4")
        use_hypotheses = ui.switch("Hypothesis Generation", value=False)
        ui.label(
            "Track scientific hypotheses across iterations — propose, test, and confirm/reject them."
        ).classes("text-sm text-gray-700 mt-1")

        ui.separator().classes("my-4")
        coinvestigate_mode = ui.switch("Coinvestigate Mode", value=False)
        ui.label(
            "Requires your active participation. After each iteration, I will pause to receive your feedback."
        ).classes("text-sm text-gray-700 mt-1")
        ui.label(
            "Requires you to stay near your computer. Auto-continues after 15 min if you don't respond."
        ).classes("text-xs text-orange-700")

        ui.button(
            "Start Discovery",
            on_click=lambda: _submit_job(
                job_manager=job_manager,
                user_can_start_jobs=user_can_start_jobs,
                session_id=session_id,
                research_question=research_question,
                max_iterations=max_iterations,
                use_hypotheses=use_hypotheses,
                coinvestigate_mode=coinvestigate_mode,
            ),
        ).classes("w-full mt-4")
