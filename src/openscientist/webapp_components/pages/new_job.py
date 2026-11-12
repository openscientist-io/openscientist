"""New job submission page."""

import logging
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Self

from nicegui import app, ui
from nicegui.events import GenericEventArguments, UploadEventArguments

from openscientist.admin_settings import current_admin_settings
from openscientist.auth import can_current_user_start_jobs, get_current_user_id, require_auth
from openscientist.exceptions import BudgetExceededError
from openscientist.providers import check_provider_config
from openscientist.upload_limits import MAX_UPLOAD_FILES_PER_REQUEST
from openscientist.webapp_components.components.banners import (
    render_config_error_banner,
    render_pending_approval_notice,
)
from openscientist.webapp_components.components.navigation import render_navigator
from openscientist.webapp_components.utils.permissions import revalidate_session
from openscientist.webapp_components.utils.session import (
    PendingUpload,
    begin_upload,
    clear_upload_storage,
    clear_uploaded_files,
    finish_upload,
    get_uploaded_files,
    remove_uploaded_files,
    save_uploaded_file,
)

logger = logging.getLogger(__name__)

app.on_startup(clear_upload_storage)
app.on_shutdown(clear_upload_storage)


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


async def _submit_job(
    *,
    job_manager: Any,
    session_id: str,
    research_question: ui.textarea,
    max_iterations: ui.number,
    use_hypotheses: ui.switch,
    coinvestigate_mode: ui.switch,
) -> None:
    """Validate input and create a new discovery job."""
    user_info = await revalidate_session()
    if user_info is None:
        ui.notify("Authentication required. Please log in again.", type="negative")
        ui.navigate.to("/login")
        return
    if not user_info["can_start_jobs"]:
        ui.notify("Your account is pending administrator approval.", type="warning")
        return

    question = research_question.value.strip()
    if not question:
        ui.notify("Please enter a research question", type="negative")
        return

    current_user_id = user_info["user_id"]

    job_id = str(uuid.uuid4())
    data_files = _persist_uploaded_files(session_id)
    mode = "coinvestigate" if coinvestigate_mode.value else "autonomous"

    try:
        await job_manager.create_job(
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
    except BudgetExceededError as exc:
        logger.warning("Job creation refused by a budget limit: %s", exc)
        ui.notify(str(exc), type="negative")
    except Exception as exc:
        logger.error("Error creating job: %s", exc, exc_info=True)
        _notify_creation_error(exc)


def _handle_upload(e: UploadEventArguments, session_id: str) -> Awaitable[None]:
    """Claim the filename before scheduling its upload handler."""
    return _save_upload(e, session_id, begin_upload(session_id, e.file))


async def _save_upload(e: UploadEventArguments, session_id: str, upload: PendingUpload) -> None:
    """Save a live upload within the page's limits."""
    try:
        name = e.file.name
        admin = await current_admin_settings()
        if e.file.size() > admin.max_file_size_mb * 1024 * 1024:
            ui.notify(
                f"{name} exceeds the maximum file size of {admin.max_file_size_mb} MB",
                type="negative",
            )
            return
        if not await save_uploaded_file(
            session_id,
            e.file,
            upload,
            max_files=MAX_UPLOAD_FILES_PER_REQUEST,
            max_file_size_bytes=admin.max_file_size_mb * 1024 * 1024,
        ):
            return
        ui.notify(f"Uploaded: {name}", type="positive")
        logger.info("Successfully uploaded %s (%d bytes)", name, e.file.size())
    except (ValueError, OSError) as exc:
        logger.error("Upload failed: %s", exc, exc_info=True)
        ui.notify(f"Upload failed: {exc}", type="negative")
    finally:
        finish_upload(session_id, upload)


class _JobUpload(ui.upload):
    def __init__(self, session_id: str, max_file_size_mb: int) -> None:
        self._upload_session_id = session_id
        super().__init__(
            label=f"Upload Data Files (Optional - Max {max_file_size_mb} MB per file)",
            multiple=True,
            auto_upload=True,
            on_upload=self._uploaded,
            max_file_size=max_file_size_mb * 1024 * 1024,
            max_files=MAX_UPLOAD_FILES_PER_REQUEST,
            on_rejected=lambda: ui.notify(
                f"Upload at most {MAX_UPLOAD_FILES_PER_REQUEST} files "
                f"up to {max_file_size_mb} MB per file",
                type="negative",
            ),
        )
        self.props(':field-name="file => encodeURIComponent(file.__key)"')
        self.on(
            "removed",
            self._removed,
            js_handler="files => emit(files.map(({__key}) => ({__key})))",
        )

    def _uploaded(self, event: UploadEventArguments) -> Awaitable[None]:
        return _handle_upload(event, self._upload_session_id)

    def _removed(self, event: GenericEventArguments) -> None:
        payload: object = event.args
        if not isinstance(payload, list):
            return
        removed: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            key = item.get("__key")
            if isinstance(key, str):
                removed.append(key)
        try:
            remove_uploaded_files(self._upload_session_id, removed)
        except OSError:
            logger.exception("Could not remove an uploaded file")
            ui.notify("Could not remove an uploaded file. Please try again.", type="negative")

    def reset(self) -> Self:
        if self.enabled:
            clear_uploaded_files(self._upload_session_id)
        return super().reset()


@ui.page("/new")
@require_auth
async def new_job_page() -> None:
    """Job submission form."""
    from openscientist import web_app

    job_manager = web_app.get_job_manager()
    user_can_start_jobs = can_current_user_start_jobs()
    user_id = get_current_user_id()
    client = ui.context.client
    session_id = _build_upload_session_id(user_id, client)
    client.on_disconnect(lambda: clear_uploaded_files(session_id))

    is_configured, provider_name, config_errors = check_provider_config()
    max_file_size_mb = (await current_admin_settings()).max_file_size_mb
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

    # The page height minus the header (4.5rem) and the page padding, so the card centres.
    with (
        ui.column()
        .classes("w-full items-center justify-center")
        .style("min-height: calc(100vh - 6.5rem)"),
        ui.card().classes("w-full max-w-2xl p-6 gap-4"),
    ):
        ui.label("Submit Discovery Job").classes("text-h6")

        research_question = (
            ui.textarea(
                label="Research Question",
                placeholder="e.g., What interesting patterns can be found in the attached data, and what biological mechanisms explain them?",
                validation={"Too short": lambda value: len(value) >= 10},
            )
            .classes("w-full")
            .props("outlined autogrow input-style={'minHeight': '72px'}")
        )

        _JobUpload(session_id, max_file_size_mb).classes("w-full os-compact-upload")

        ui.separator()

        with ui.row().classes("w-full items-start gap-x-8 gap-y-4"):
            max_iterations = (
                ui.number(
                    label="Max Iterations",
                    value=10,
                    min=2,
                    max=100,
                    step=1,
                )
                .classes("w-28")
                .props("outlined dense")
            )

            with ui.column().classes("flex-1 gap-3"):
                with ui.column().classes("gap-0"):
                    use_hypotheses = ui.switch("Hypothesis Generation", value=False)
                    ui.label("Track scientific hypotheses across iterations.").classes(
                        "text-sm text-gray-600 ml-12"
                    )

                with ui.column().classes("gap-0"):
                    coinvestigate_mode = ui.switch("Coinvestigate Mode", value=False)
                    ui.label("Pauses after each iteration for your feedback.").classes(
                        "text-sm text-gray-600 ml-12"
                    )
                    ui.label(
                        "Stay near your computer. Auto-continues after 15 min without a response."
                    ).classes("text-xs text-orange-700 ml-12")

        with ui.row().classes("w-full justify-end"):

            async def on_submit() -> None:
                if not submit_button.enabled:
                    return
                submit_button.disable()
                try:
                    await _submit_job(
                        job_manager=job_manager,
                        session_id=session_id,
                        research_question=research_question,
                        max_iterations=max_iterations,
                        use_hypotheses=use_hypotheses,
                        coinvestigate_mode=coinvestigate_mode,
                    )
                finally:
                    submit_button.enable()

            submit_button = ui.button("Start Discovery", on_click=on_submit).props("color=primary")
