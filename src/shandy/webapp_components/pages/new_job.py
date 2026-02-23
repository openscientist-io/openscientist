"""New job submission page."""

import logging
import tempfile
import uuid
from pathlib import Path

from nicegui import ui

from shandy.auth import get_current_user_id, require_auth
from shandy.providers import check_provider_config
from shandy.webapp_components.ui_components import (
    render_config_error_banner,
    render_navigator,
)
from shandy.webapp_components.utils.session import (
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


@ui.page("/new")
@require_auth
def new_job_page():
    """Job submission form."""
    # Import module to access global job_manager at runtime
    from shandy import web_app

    job_manager = web_app.get_job_manager()

    # Isolate uploads per connected client and authenticated user.
    user_id = get_current_user_id()
    client = ui.context.client
    session_id = _build_upload_session_id(user_id, client)

    # Ensure uploads are cleared when the websocket disconnects.
    client.on_disconnect(lambda: clear_uploaded_files(session_id))

    def submit_job():
        """Handle job submission."""
        # Validate inputs
        if not research_question.value.strip():
            ui.notify("Please enter a research question", type="negative")
            return

        # Files are optional - no validation needed

        # Generate job ID (full UUID for database compatibility)
        job_uuid = uuid.uuid4()
        job_id = str(job_uuid)

        # Save uploaded files to temp location (if any)
        data_files = []
        uploaded_files = get_uploaded_files(session_id)
        if uploaded_files:
            for uploaded_file in uploaded_files:
                # Create temp file
                temp_file = Path(tempfile.mkdtemp()) / uploaded_file["name"]
                with open(temp_file, "wb") as f:
                    f.write(uploaded_file["content"])
                data_files.append(temp_file)

        # Create job
        try:
            # Get current user ID
            current_user_id = get_current_user_id()

            if not current_user_id:
                ui.notify("Authentication required. Please log in again.", type="negative")
                ui.navigate.to("/login")
                return

            # Determine investigation mode
            mode = "coinvestigate" if coinvestigate_mode.value else "autonomous"

            job_manager.create_job(
                job_id=job_id,
                research_question=research_question.value,
                data_files=data_files,
                max_iterations=int(max_iterations.value),
                use_skills=use_skills_toggle.value,
                auto_start=True,
                investigation_mode=mode,
                owner_id=current_user_id,  # Associate job with current user
            )

            ui.notify(f"Job {job_id} created and started!", type="positive")

            # Clear session-stored uploaded files
            clear_uploaded_files(session_id)

            # Redirect to job detail page (no need to clear form since we're navigating away)
            ui.navigate.to(f"/job/{job_id}")

        except Exception as e:
            # Show user-friendly error message
            error_msg = str(e)
            if "authentication" in error_msg.lower() or "api key" in error_msg.lower():
                ui.notify(
                    "Authentication error. Please contact your administrator to check API credentials.",
                    type="negative",
                )
            elif "event loop" in error_msg.lower():
                ui.notify(
                    "Internal server error. Please try again or contact support.",
                    type="negative",
                )
            else:
                ui.notify(
                    "Error creating job. Please try again or contact support.", type="negative"
                )
            logger.error("Error creating job: %s", e, exc_info=True)

    async def handle_upload(e):
        """Handle file upload."""
        try:
            # e.file is the uploaded file object
            # e.file.read() is async and returns bytes
            content = await e.file.read()
            name = e.file.name

            add_uploaded_file(session_id, name, content)
            ui.notify(f"Uploaded: {name}", type="positive")
            logger.info("Successfully uploaded %s (%d bytes)", name, len(content))
        except (ValueError, OSError) as ex:
            logger.error("Upload failed: %s", ex, exc_info=True)
            ui.notify(f"Upload failed: {str(ex)}", type="negative")

    # Check provider configuration
    is_configured, provider_name, config_errors = check_provider_config()

    # Page header with navigation
    render_navigator(
        active_page="new",
        show_new_job=is_configured,
    )

    # Show configuration error if provider is not set up
    if not is_configured:
        render_config_error_banner(provider_name, config_errors, show_back_button=True)
        return  # Don't render the form

    # Main content
    with ui.card().classes("w-full max-w-2xl mx-auto mt-8"):
        ui.label("Submit Discovery Job").classes("text-h5 mb-4")

        # Research question
        research_question = ui.textarea(
            label="Research Question",
            placeholder="e.g., What metabolic pathways are affected by hypothermia?",
            validation={"Too short": lambda value: len(value) >= 10},
        ).classes("w-full")

        # File upload
        # Supported: Tabular (CSV, TSV, Excel, Parquet, JSON), Structures (PDB, mmCIF), Sequences (FASTA), Images (PNG, JPG)
        ui.upload(
            label="Upload Data Files (Optional - Tabular, Structures, Sequences, Images)",
            multiple=True,
            auto_upload=True,
            on_upload=handle_upload,
        ).classes("w-full")

        # Configuration
        max_iterations = ui.number(
            label="Max Iterations", value=10, min=2, max=100, step=1
        ).classes("w-full")

        # Advanced options (collapsed by default)
        with ui.expansion("Advanced Options (Experimental)", icon="science").classes("w-full mt-4"):
            with ui.card().classes("w-full"):
                # Skills toggle
                use_skills_toggle = ui.switch("Enable Skills", value=True)
                ui.label(
                    "Skills provide domain-specific guidance (e.g., statistical methods, analysis workflows)."
                ).classes("text-sm text-gray-700 mt-1")

                ui.separator().classes("my-4")

                # Coinvestigate mode toggle
                coinvestigate_mode = ui.switch("Coinvestigate Mode", value=False)
                ui.label(
                    "Requires your active participation. After each iteration, I will pause to receive your feedback."
                ).classes("text-sm text-gray-700 mt-1")
                ui.label(
                    "Requires you to stay near your computer. Auto-continues after 15 min if you don't respond."
                ).classes("text-xs text-orange-700")

        # Submit button
        ui.button("Start Discovery", on_click=submit_job).classes("w-full mt-4")
