"""Jobs list page."""

import logging
from uuid import UUID

from nicegui import ui
from sqlalchemy import select

from shandy.auth import (
    can_current_user_start_jobs,
    get_current_user_id,
    is_current_user_admin,
    require_auth,
)
from shandy.database.models import Job, JobShare, User
from shandy.database.rls import set_current_user
from shandy.database.session import get_admin_session, get_session_ctx
from shandy.job.types import JobStatus
from shandy.providers import check_provider_config
from shandy.webapp_components.ui_components import (
    render_actions_slot_with_delete,
    render_config_error_banner,
    render_delete_dialog,
    render_job_id_slot,
    render_navigator,
    render_pending_approval_notice,
    render_permission_badge_slot,
    render_share_dialog,
    render_stat_badges,
    render_status_cell_slot,
)
from shandy.webapp_components.utils import setup_timer_cleanup

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

    # Track active timers for cleanup on disconnect
    _active_timers = setup_timer_cleanup()

    # Capture admin status and user ID for access control
    current_user_is_admin = is_current_user_admin()
    current_user_id = get_current_user_id()
    can_start_jobs = can_current_user_start_jobs()

    async def refresh_jobs(table_to_update):
        """Refresh jobs table from database with RLS."""
        try:
            async with get_session_ctx() as session:
                await set_current_user(session, UUID(current_user_id))

                stmt = (
                    select(Job)
                    .where(Job.owner_id == UUID(current_user_id))
                    .order_by(Job.created_at.desc())
                )
                result = await session.execute(stmt)
                db_jobs = result.scalars().all()

            # Augment with real-time progress from knowledge_state.json
            jobs = [job_manager._db_model_to_job_info(m) for m in db_jobs]

            # Update summary badges
            status_counts: dict[str, int] = {}
            for s in JobStatus:
                status_counts[s.value] = sum(1 for j in jobs if j.status == s)

            badges_container.clear()
            with badges_container:
                render_stat_badges(
                    [
                        ("Total Jobs", len(jobs), ""),
                        ("Running", status_counts.get("running", 0), "blue"),
                        ("Completed", status_counts.get("completed", 0), "green"),
                    ]
                )

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
                    "can_share": True,  # Users can always share their own jobs
                    "can_delete": True,  # Users can always delete their own jobs
                }
                for job in jobs
            ]
            table_to_update.update()
        except Exception as e:
            logger.error("Failed to load jobs: %s", e)

    async def refresh_shared_jobs(table_to_update):
        """Refresh shared jobs table from database."""
        try:
            # Use admin session to bypass RLS on the users table join
            # (RLS on users only allows seeing own record, but we need
            # to see the job owner's name). Authorization is handled by
            # the WHERE clause filtering on current_user_id.
            async with get_admin_session() as session:
                # Get jobs shared with current user
                stmt = (
                    select(Job, User, JobShare)
                    .join(JobShare, Job.id == JobShare.job_id)
                    .join(User, Job.owner_id == User.id)
                    .where(JobShare.shared_with_user_id == UUID(current_user_id))
                    .order_by(Job.updated_at.desc())
                )
                result = await session.execute(stmt)
                shared_jobs = result.all()

                # Augment with real-time progress from knowledge_state.json
                rows = []
                for job, owner, share in shared_jobs:
                    job_info = job_manager._db_model_to_job_info(job)
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
                            # Users cannot share jobs they don't own
                            "can_share": False,
                            # Only admins can delete shared jobs
                            "can_delete": current_user_is_admin,
                        }
                    )

                table_to_update.rows = rows
                table_to_update.update()
        except Exception as e:
            logger.error("Failed to load shared jobs: %s", e)

    def show_delete_dialog(job_id: str, table_to_refresh, is_shared: bool = False):
        """Show confirmation dialog for deleting a job."""

        async def on_deleted():
            if is_shared:
                await refresh_shared_jobs(table_to_refresh)
            else:
                await refresh_jobs(table_to_refresh)

        dialog = render_delete_dialog(job_id, job_manager, on_deleted=on_deleted)
        dialog.open()

    def show_share_dialog(job_id: str):
        """Show dialog for sharing a job with other users."""
        dialog = render_share_dialog(job_id)
        dialog.open()

    # Page header with navigation
    render_navigator(active_page="jobs", show_new_job=is_configured)

    # Show configuration error banner if provider is not configured
    if not is_configured:
        render_config_error_banner(provider_name, config_errors)

    if not can_start_jobs:
        render_pending_approval_notice()

    # Summary badges (populated async by refresh_jobs)
    badges_container = ui.row().classes("w-full")

    # Tabs for My Jobs vs Shared with me
    with ui.tabs().classes("w-full") as tabs:
        my_jobs_tab = ui.tab("My Jobs", icon="work")
        shared_tab = ui.tab("Shared with me", icon="people")

    with ui.tab_panels(tabs, value=my_jobs_tab).classes("w-full"):
        # ===== MY JOBS TAB =====
        with ui.tab_panel(my_jobs_tab):
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

            # Add job ID column slot with clickable badges
            my_jobs_table.add_slot("body-cell-job_id", render_job_id_slot())

            # Add status column slot with enhanced styling for failed jobs
            my_jobs_table.add_slot("body-cell-status", render_status_cell_slot())

            # Add action buttons with share and delete icons
            my_jobs_table.add_slot("body-cell-actions", render_actions_slot_with_delete())

            # Handle job ID badge clicks and action buttons
            my_jobs_table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))
            my_jobs_table.on("share-job", lambda e: show_share_dialog(e.args))
            my_jobs_table.on(
                "delete-job",
                lambda e: show_delete_dialog(e.args, my_jobs_table, is_shared=False),
            )

            # Async wrapper for timer
            async def refresh_my_jobs_table():
                await refresh_jobs(my_jobs_table)

            # Initial load
            my_jobs_init_timer = ui.timer(0.1, refresh_my_jobs_table, once=True)
            _active_timers.append(my_jobs_init_timer)

            # Auto-refresh via websocket (no page reload)
            my_jobs_timer = ui.timer(5.0, refresh_my_jobs_table)
            _active_timers.append(my_jobs_timer)

        # ===== SHARED WITH ME TAB =====
        with ui.tab_panel(shared_tab):
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

            # Add job ID column slot with clickable badges
            shared_jobs_table.add_slot("body-cell-job_id", render_job_id_slot())

            # Add status column slot
            shared_jobs_table.add_slot("body-cell-status", render_status_cell_slot())

            # Add permission badge slot
            shared_jobs_table.add_slot(
                "body-cell-permission",
                render_permission_badge_slot(),
            )

            # Add action buttons with share and delete icons
            shared_jobs_table.add_slot("body-cell-actions", render_actions_slot_with_delete())

            # Handle job ID badge clicks and action buttons
            shared_jobs_table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))
            shared_jobs_table.on(
                "delete-job",
                lambda e: show_delete_dialog(e.args, shared_jobs_table, is_shared=True),
            )

            # Create async wrapper for timer (lambda + async doesn't work correctly)
            async def refresh_shared_table():
                await refresh_shared_jobs(shared_jobs_table)

            # Initial load
            shared_init_timer = ui.timer(0.1, refresh_shared_table, once=True)
            _active_timers.append(shared_init_timer)

            # Auto-refresh via websocket (no page reload)
            shared_refresh_timer = ui.timer(5.0, refresh_shared_table)
            _active_timers.append(shared_refresh_timer)
