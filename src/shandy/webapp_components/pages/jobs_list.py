"""Jobs list page."""

from nicegui import ui

from shandy.webapp_components.ui_components import render_status_cell_slot
from shandy.webapp_components.utils.auth import require_auth


@ui.page("/jobs")
@require_auth
def jobs_page():
    """Jobs list page."""
    # Import module to access global job_manager at runtime
    from shandy import web_app

    job_manager = web_app.get_job_manager()

    def refresh_jobs():
        """Refresh jobs table."""
        jobs = job_manager.list_jobs()

        # Update table
        table.rows = [
            {
                "job_id": job.job_id,
                "question": (
                    job.research_question[:50] + "..."
                    if len(job.research_question) > 50
                    else job.research_question
                ),
                "status": job.status.value,
                "error": job.error or "",  # Include error for tooltip
                "iterations": f"{job.iterations_completed}/{job.max_iterations}",
                "findings": job.findings_count,
                "created": job.created_at[:19],  # Remove milliseconds
            }
            for job in jobs
        ]
        table.update()

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY - Jobs").classes("text-h4")
        with ui.row():
            ui.button("New Job", on_click=lambda: ui.navigate.to("/new"), icon="add")
            ui.button("Refresh", on_click=refresh_jobs, icon="refresh")
            ui.button(
                "Billing", on_click=lambda: ui.navigate.to("/billing"), icon="payments"
            ).props("flat")

    # Summary cards
    summary = job_manager.get_job_summary()
    with ui.row().classes("w-full gap-4 p-4"):
        with ui.card():
            ui.label("Total Jobs").classes("text-subtitle2")
            ui.label(str(summary["total_jobs"])).classes("text-h4")

        with ui.card():
            ui.label("Running").classes("text-subtitle2")
            ui.label(str(summary["status_counts"].get("running", 0))).classes(
                "text-h4 text-blue-600"
            )

        with ui.card():
            ui.label("Completed").classes("text-subtitle2")
            ui.label(str(summary["status_counts"].get("completed", 0))).classes(
                "text-h4 text-green-600"
            )

    # Jobs table
    table = ui.table(
        columns=[
            {"name": "job_id", "label": "Job ID", "field": "job_id", "align": "left"},
            {
                "name": "question",
                "label": "Research Question",
                "field": "question",
                "align": "left",
            },
            {"name": "status", "label": "Status", "field": "status", "align": "center"},
            {
                "name": "iterations",
                "label": "Iterations",
                "field": "iterations",
                "align": "center",
            },
            {
                "name": "findings",
                "label": "Findings",
                "field": "findings",
                "align": "center",
            },
            {
                "name": "created",
                "label": "Created",
                "field": "created",
                "align": "left",
            },
            {
                "name": "actions",
                "label": "Actions",
                "field": "actions",
                "align": "center",
            },
        ],
        rows=[],
        row_key="job_id",
        pagination=10,
    ).classes("w-full")

    # Add status column slot with enhanced styling for failed jobs
    table.add_slot("body-cell-status", render_status_cell_slot())

    # Add action buttons using slot template
    table.add_slot(
        "body-cell-actions",
        r"""
        <q-td :props="props">
            <q-btn flat dense color="primary" label="View"
                   @click="$parent.$emit('view-job', props.row.job_id)" />
        </q-td>
    """,
    )

    table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))

    # Initial load
    refresh_jobs()
