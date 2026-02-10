"""New job submission page."""

import logging
import tempfile
import uuid
from pathlib import Path

from nicegui import ui

from shandy.auth import get_current_user_id, require_auth
from shandy.webapp_components.utils.session import (
    add_uploaded_file,
    clear_uploaded_files,
    get_uploaded_files,
)

logger = logging.getLogger(__name__)


@ui.page("/new")
@require_auth
def new_job_page():
    """Job submission form."""
    # Import module to access global job_manager at runtime
    from shandy import web_app
    from shandy.webapp_components.pages.index import index_page

    job_manager = web_app.get_job_manager()

    # Use client ID as key for this session's uploads
    session_id = str(id(index_page))  # Simple session identifier

    def submit_job():
        """Handle job submission."""
        # Validate inputs
        if not research_question.value.strip():
            ui.notify("Please enter a research question", type="negative")
            return

        # Files are optional - no validation needed

        # Generate job ID
        job_id = f"job_{uuid.uuid4().hex[:8]}"

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
            user_id = get_current_user_id()

            # Determine investigation mode
            mode = "coinvestigate" if coinvestigate_mode.value else "autonomous"

            job_manager.create_job(
                job_id=job_id,
                research_question=research_question.value,
                data_files=data_files,
                max_iterations=int(max_iterations.value),
                use_skills=True,
                auto_start=True,
                investigation_mode=mode,
                owner_id=user_id,  # Associate job with current user
            )

            ui.notify(f"Job {job_id} created and started!", type="positive")

            # Clear form
            research_question.value = ""
            clear_uploaded_files(session_id)
            upload.reset()

            # Redirect to job detail page
            ui.navigate.to(f"/job/{job_id}")

        except (ValueError, OSError) as e:
            ui.notify(f"Error creating job: {e}", type="negative")
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

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY").classes("text-h4")
        ui.label("Scientific Hypothesis Agent for Novel Discovery").classes("text-subtitle1")

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
        upload = ui.upload(
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
                coinvestigate_mode = ui.switch("Coinvestigate Mode", value=False)
                ui.label(
                    "Requires your active participation. After each iteration, I will pause to receive your feedback."
                ).classes("text-sm text-gray-700 mt-1")
                ui.label(
                    "Requires you to stay near your computer. Auto-continues after 15 min if you don't respond."
                ).classes("text-xs text-orange-700")

        # Submit button
        ui.button("Start Discovery", on_click=submit_job).classes("w-full mt-4")

    # Quick links
    with ui.row().classes("w-full max-w-2xl mx-auto mt-4"):
        ui.button("View Jobs", on_click=lambda: ui.navigate.to("/jobs"), icon="list").classes(
            "flex-1"
        )
        ui.button("Documentation", on_click=lambda: ui.navigate.to("/docs"), icon="help").classes(
            "flex-1"
        )
