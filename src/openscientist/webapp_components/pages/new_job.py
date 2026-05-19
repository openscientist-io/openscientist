"""New job submission page."""

import logging
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nicegui import ui

from openscientist.auth import can_current_user_start_jobs, get_current_user_id, require_auth
from openscientist.job_templates import (
    FREEFORM_TEMPLATE_ID,
    TemplateField,
    TemplateValidationError,
    build_template_research_question,
    default_max_iterations_for_template,
    list_job_templates,
    normalize_template_id,
    resolve_template_submission,
    workflow_options,
)
from openscientist.providers import check_provider_config
from openscientist.webapp_components.ui_components import (
    render_config_error_banner,
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


def _widget_value(widget: Any) -> Any:
    """Read a NiceGUI widget value for template input collection."""
    return getattr(widget, "value", None)


def _collect_template_inputs(
    template_id: str | None,
    template_field_widgets: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Collect structured inputs for the selected guided workflow."""
    normalized_template_id = normalize_template_id(template_id)
    if normalized_template_id is None:
        return None

    widgets = template_field_widgets.get(normalized_template_id, {})
    return {
        key: value
        for key, widget in widgets.items()
        if (value := _widget_value(widget)) not in (None, "")
    }


def _render_template_field(field: TemplateField) -> Any:
    """Render one template input field."""
    default_value = field.default or ""
    widget: Any
    if field.kind == "textarea":
        widget = ui.textarea(
            label=field.label,
            placeholder=field.placeholder,
            value=default_value,
        ).props(f"rows={field.rows}")
    elif field.kind == "select":
        widget = ui.select(
            options=field.option_labels(),
            value=field.default,
            label=field.label,
        )
    else:
        widget = ui.input(
            label=field.label,
            placeholder=field.placeholder,
            value=default_value,
        )

    widget.classes("w-full")
    if field.help_text:
        ui.label(field.help_text).classes("text-xs text-gray-600 -mt-2")
    return widget


def _submit_job(
    *,
    job_manager: Any,
    user_can_start_jobs: bool,
    session_id: str,
    research_question: ui.textarea,
    max_iterations: ui.number,
    use_hypotheses: ui.switch,
    coinvestigate_mode: ui.switch,
    workflow_template: Any | None = None,
    template_field_widgets: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Validate input and create a new discovery job."""
    if not user_can_start_jobs:
        ui.notify("Your account is pending administrator approval.", type="warning")
        return

    selected_template_id = normalize_template_id(
        str(getattr(workflow_template, "value", FREEFORM_TEMPLATE_ID))
    )
    collected_template_inputs = _collect_template_inputs(
        selected_template_id,
        template_field_widgets or {},
    )

    try:
        template_resolution = resolve_template_submission(
            template_id=selected_template_id,
            template_inputs=collected_template_inputs,
            research_question=research_question.value,
        )
    except TemplateValidationError as exc:
        ui.notify(str(exc), type="negative")
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
            research_question=template_resolution.research_question,
            data_files=data_files,
            max_iterations=int(max_iterations.value),
            use_hypotheses=use_hypotheses.value,
            auto_start=True,
            investigation_mode=mode,
            owner_id=current_user_id,
            template_id=template_resolution.template_id,
            template_inputs=template_resolution.template_inputs,
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

        templates = list_job_templates()
        workflow_template = ui.select(
            options=workflow_options(),
            value=FREEFORM_TEMPLATE_ID,
            label="Workflow",
        ).classes("w-full")

        template_field_widgets: dict[str, dict[str, Any]] = {}
        template_sections: dict[str, Any] = {}
        for template in templates:
            with ui.column().classes("w-full gap-3 hidden") as section:
                ui.label(template.summary).classes("text-sm text-gray-700")
                template_field_widgets[template.id] = {}
                for field in template.fields:
                    template_field_widgets[template.id][field.key] = _render_template_field(field)
            template_sections[template.id] = section

        research_question = ui.textarea(
            label="Research Question",
            placeholder="e.g., What metabolic pathways are affected by hypothermia?",
            validation={"Too short": lambda value: len(value) >= 10},
        ).classes("w-full")

        template_state = {"syncing_question": False, "question_synced_to_template": True}

        def update_generated_question(*, show_errors: bool = False) -> bool:
            selected = normalize_template_id(str(workflow_template.value))
            if selected is None:
                return False
            template_inputs = _collect_template_inputs(selected, template_field_widgets)
            try:
                generated_question = build_template_research_question(selected, template_inputs)
            except TemplateValidationError as exc:
                if show_errors:
                    ui.notify(str(exc), type="warning")
                return False

            template_state["syncing_question"] = True
            research_question.value = generated_question
            research_question.update()
            template_state["syncing_question"] = False
            template_state["question_synced_to_template"] = True
            return True

        regenerate_button = (
            ui.button(
                "Regenerate Research Question",
                on_click=lambda: update_generated_question(show_errors=True),
            )
            .props("outline color=primary")
            .classes("w-full hidden")
        )

        def update_template_visibility() -> None:
            selected = str(workflow_template.value or FREEFORM_TEMPLATE_ID)
            guided_selected = normalize_template_id(selected) is not None
            for template_id, section in template_sections.items():
                if template_id == selected:
                    section.classes(remove="hidden")
                else:
                    section.classes(add="hidden")

            if guided_selected:
                regenerate_button.classes(remove="hidden")
                template_state["question_synced_to_template"] = True
                update_generated_question()
            else:
                regenerate_button.classes(add="hidden")

        def maybe_update_template_question() -> None:
            if template_state["question_synced_to_template"]:
                update_generated_question()

        def mark_question_edited() -> None:
            if not template_state["syncing_question"]:
                template_state["question_synced_to_template"] = False

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

        iteration_state = {"syncing_iterations": False, "user_edited_iterations": False}

        def update_default_iterations() -> None:
            if iteration_state["user_edited_iterations"]:
                return

            iteration_state["syncing_iterations"] = True
            max_iterations.value = default_max_iterations_for_template(
                str(workflow_template.value or FREEFORM_TEMPLATE_ID)
            )
            max_iterations.update()
            iteration_state["syncing_iterations"] = False

        def mark_iterations_edited() -> None:
            if not iteration_state["syncing_iterations"]:
                iteration_state["user_edited_iterations"] = True

        def handle_workflow_template_change() -> None:
            update_template_visibility()
            update_default_iterations()

        workflow_template.on("update:model-value", lambda _event: handle_workflow_template_change())
        research_question.on("update:model-value", lambda _event: mark_question_edited())
        max_iterations.on("update:model-value", lambda _event: mark_iterations_edited())
        for widgets in template_field_widgets.values():
            for widget in widgets.values():
                widget.on("update:model-value", lambda _event: maybe_update_template_question())

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
                workflow_template=workflow_template,
                template_field_widgets=template_field_widgets,
            ),
        ).classes("w-full mt-4")
