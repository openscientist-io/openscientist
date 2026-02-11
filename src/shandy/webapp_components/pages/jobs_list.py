"""Jobs list page."""

import logging

from nicegui import ui

from shandy.auth import get_current_user_id, require_auth
from shandy.providers import check_provider_config
from shandy.webapp_components.ui_components import render_status_cell_slot

logger = logging.getLogger(__name__)


@ui.page("/jobs")
@require_auth
def jobs_page():
    """Jobs list page."""
    # Import module to access global job_manager at runtime
    from shandy import web_app

    job_manager = web_app.get_job_manager()

    # Check provider configuration
    is_configured, provider_name, config_errors = check_provider_config()

    def refresh_jobs(table_to_update):
        """Refresh jobs table."""
        jobs = job_manager.list_jobs()

        # Update table
        table_to_update.rows = [
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
        table_to_update.update()

    async def refresh_shared_jobs(table_to_update):
        """Refresh shared jobs table from database."""
        from uuid import UUID

        from sqlalchemy import select

        from shandy.database.models import Job, JobShare, User
        from shandy.database.rls import set_current_user
        from shandy.database.session import get_session

        try:
            # Get current user ID
            user_id = get_current_user_id()

            # Query shared jobs from database
            async for session in get_session():
                try:
                    await set_current_user(session, UUID(user_id))

                    # Get jobs shared with current user
                    stmt = (
                        select(Job, User, JobShare)
                        .join(JobShare, Job.id == JobShare.job_id)
                        .join(User, Job.owner_id == User.id)
                        .where(JobShare.shared_with_user_id == UUID(user_id))
                        .order_by(Job.updated_at.desc())
                    )
                    result = await session.execute(stmt)
                    shared_jobs = result.all()

                    # Get job info from job_manager for each shared job
                    rows = []
                    for job, owner, share in shared_jobs:
                        job_info = job_manager.get_job(str(job.id))
                        if job_info:
                            rows.append(
                                {
                                    "job_id": str(job.id),
                                    "question": (
                                        job_info.research_question[:50] + "..."
                                        if len(job_info.research_question) > 50
                                        else job_info.research_question
                                    ),
                                    "owner": owner.name,
                                    "permission": share.permission_level,
                                    "status": job_info.status.value,
                                    "error": job_info.error or "",
                                    "iterations": f"{job_info.iterations_completed}/{job_info.max_iterations}",
                                    "findings": job_info.findings_count,
                                    "created": job_info.created_at[:19],
                                }
                            )

                    table_to_update.rows = rows
                    table_to_update.update()

                except Exception as e:
                    logger.error("Failed to load shared jobs: %s", e)
                finally:
                    break  # Exit the async generator
        except Exception as e:
            logger.error("Failed to get database session: %s", e)

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY - Jobs").classes("text-h4")
        with ui.row():
            new_job_btn = ui.button(
                "New Job", on_click=lambda: ui.navigate.to("/new"), icon="add"
            )
            if not is_configured:
                new_job_btn.disable()
                new_job_btn.tooltip("Server not configured - cannot start jobs")
            ui.button(
                "Billing", on_click=lambda: ui.navigate.to("/billing"), icon="payments"
            ).props("flat")
            ui.button(
                "Admin",
                on_click=lambda: ui.navigate.to("/admin"),
                icon="admin_panel_settings",
            ).props("flat color=secondary")

    # Show configuration error banner if provider is not configured
    if not is_configured:
        with ui.card().classes("w-full mx-4 mt-4 bg-red-50 border-l-4 border-red-500"):
            with ui.row().classes("items-center gap-3"):
                ui.icon("error", color="red", size="md")
                with ui.column().classes("gap-1"):
                    ui.label("Server Configuration Error").classes(
                        "text-red-800 font-bold"
                    )
                    ui.label(
                        f"The {provider_name.upper()} provider is not configured correctly. "
                        "Jobs cannot be started until this is resolved."
                    ).classes("text-red-700")
                    ui.label("Please contact the system administrator.").classes(
                        "text-red-600 text-sm"
                    )
            with ui.expansion("Technical Details", icon="info").classes("mt-2"):
                for error in config_errors:
                    ui.label(f"• {error}").classes("text-red-600 text-sm font-mono")

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

    # Tabs for My Jobs vs Shared with me
    with ui.tabs().classes("w-full") as tabs:
        my_jobs_tab = ui.tab("My Jobs", icon="work")
        shared_tab = ui.tab("Shared with me", icon="people")

    with ui.tab_panels(tabs, value=my_jobs_tab).classes("w-full"):
        # ===== MY JOBS TAB =====
        with ui.tab_panel(my_jobs_tab):
            with ui.row().classes("w-full justify-end mb-2"):
                ui.button(
                    "Refresh",
                    on_click=lambda: refresh_jobs(my_jobs_table),
                    icon="refresh",
                )

            # My jobs table
            my_jobs_table = ui.table(
                columns=[
                    {
                        "name": "job_id",
                        "label": "Job ID",
                        "field": "job_id",
                        "align": "left",
                    },
                    {
                        "name": "question",
                        "label": "Research Question",
                        "field": "question",
                        "align": "left",
                    },
                    {
                        "name": "status",
                        "label": "Status",
                        "field": "status",
                        "align": "center",
                    },
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
            my_jobs_table.add_slot("body-cell-status", render_status_cell_slot())

            # Add action buttons using slot template
            my_jobs_table.add_slot(
                "body-cell-actions",
                r"""
                <q-td :props="props">
                    <q-btn flat dense color="primary" label="View"
                           @click="$parent.$emit('view-job', props.row.job_id)" />
                </q-td>
            """,
            )

            my_jobs_table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))

            # Initial load
            refresh_jobs(my_jobs_table)

        # ===== SHARED WITH ME TAB =====
        with ui.tab_panel(shared_tab):
            with ui.row().classes("w-full justify-end mb-2"):
                ui.button(
                    "Refresh",
                    on_click=lambda: refresh_shared_jobs(shared_jobs_table),
                    icon="refresh",
                )

            # Shared jobs table (includes owner and permission columns)
            shared_jobs_table = ui.table(
                columns=[
                    {
                        "name": "job_id",
                        "label": "Job ID",
                        "field": "job_id",
                        "align": "left",
                    },
                    {
                        "name": "question",
                        "label": "Research Question",
                        "field": "question",
                        "align": "left",
                    },
                    {
                        "name": "owner",
                        "label": "Owner",
                        "field": "owner",
                        "align": "left",
                    },
                    {
                        "name": "permission",
                        "label": "Permission",
                        "field": "permission",
                        "align": "center",
                    },
                    {
                        "name": "status",
                        "label": "Status",
                        "field": "status",
                        "align": "center",
                    },
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

            # Add status column slot
            shared_jobs_table.add_slot("body-cell-status", render_status_cell_slot())

            # Add permission badge slot
            shared_jobs_table.add_slot(
                "body-cell-permission",
                r"""
                <q-td :props="props">
                    <q-badge :color="props.row.permission === 'edit' ? 'orange' : 'blue'">
                        {{ props.row.permission }}
                    </q-badge>
                </q-td>
            """,
            )

            # Add action buttons
            shared_jobs_table.add_slot(
                "body-cell-actions",
                r"""
                <q-td :props="props">
                    <q-btn flat dense color="primary" label="View"
                           @click="$parent.$emit('view-job', props.row.job_id)" />
                </q-td>
            """,
            )

            shared_jobs_table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))

            # Initial load
            ui.timer(0.1, lambda: refresh_shared_jobs(shared_jobs_table), once=True)
