"""Jobs list page."""

import logging
from collections.abc import Awaitable, Callable
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

_OWNED_COLUMNS = [
    {"name": "job_id", "label": "Job ID", "field": "job_id", "align": "left"},
    {"name": "question", "label": "Research Question", "field": "question", "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "align": "center"},
    {"name": "iterations", "label": "Iterations", "field": "iterations", "align": "center"},
    {"name": "findings", "label": "Findings", "field": "findings", "align": "center"},
    {"name": "created", "label": "Created", "field": "created", "align": "left"},
    {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
]

_SHARED_COLUMNS = [
    {"name": "job_id", "label": "Job ID", "field": "job_id", "align": "left"},
    {"name": "question", "label": "Research Question", "field": "question", "align": "left"},
    {"name": "owner", "label": "Owner", "field": "owner", "align": "left"},
    {"name": "permission", "label": "Permission", "field": "permission", "align": "center"},
    {"name": "status", "label": "Status", "field": "status", "align": "center"},
    {"name": "iterations", "label": "Iterations", "field": "iterations", "align": "center"},
    {"name": "findings", "label": "Findings", "field": "findings", "align": "center"},
    {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
]


def _truncate_question(question: str, limit: int = 50) -> str:
    """Truncate long research question text for table display."""
    if len(question) > limit:
        return question[:limit] + "..."
    return question


def _render_badges(badges_container: ui.element, jobs: list) -> None:
    """Render aggregate job counters."""
    status_counts = {
        status.value: sum(1 for job in jobs if job.status == status) for status in JobStatus
    }
    badges_container.clear()
    with badges_container:
        render_stat_badges(
            [
                ("Total Jobs", len(jobs), ""),
                ("Running", status_counts.get("running", 0), "blue"),
                ("Completed", status_counts.get("completed", 0), "green"),
            ]
        )


async def _fetch_owned_jobs(current_user_id: str) -> list[Job]:
    """Fetch jobs owned by current user under RLS."""
    async with get_session_ctx() as session:
        user_uuid = UUID(current_user_id)
        await set_current_user(session, user_uuid)
        result = await session.execute(
            select(Job).where(Job.owner_id == user_uuid).order_by(Job.created_at.desc())
        )
        return list(result.scalars().all())


async def _fetch_shared_jobs(current_user_id: str) -> list[tuple[Job, User, JobShare]]:
    """Fetch jobs shared with current user (owner join requires admin session)."""
    async with get_admin_session() as session:
        result = await session.execute(
            select(Job, User, JobShare)
            .join(JobShare, Job.id == JobShare.job_id)
            .join(User, Job.owner_id == User.id)
            .where(JobShare.shared_with_user_id == UUID(current_user_id))
            .order_by(Job.updated_at.desc())
        )
        return list(result.tuples().all())


def _owned_rows(job_manager, db_jobs: list[Job]) -> list[dict]:
    """Build owned-jobs table rows."""
    jobs = [job_manager._db_model_to_job_info(model) for model in db_jobs]
    return [
        {
            "job_id": job.job_id,
            "question": _truncate_question(job.research_question),
            "status": job.status.value,
            "error": job.error or "",
            "iterations": f"{job.iterations_completed}/{job.max_iterations}",
            "findings": job.findings_count,
            "created": job.created_at[:19],
            "can_share": True,
            "can_delete": True,
        }
        for job in jobs
    ]


def _shared_rows(
    job_manager,
    shared_jobs: list[tuple[Job, User, JobShare]],
    current_user_is_admin: bool,
) -> list[dict]:
    """Build shared-jobs table rows."""
    rows: list[dict] = []
    for job, owner, share in shared_jobs:
        job_info = job_manager._db_model_to_job_info(job)
        rows.append(
            {
                "job_id": str(job.id),
                "question": _truncate_question(job_info.research_question),
                "owner": owner.name,
                "permission": share.permission_level,
                "status": job_info.status.value,
                "error": job_info.error or "",
                "iterations": f"{job_info.iterations_completed}/{job_info.max_iterations}",
                "findings": job_info.findings_count,
                "created": job_info.created_at[:19],
                "can_share": False,
                "can_delete": current_user_is_admin,
            }
        )
    return rows


async def _refresh_owned_jobs(
    *,
    job_manager,
    current_user_id: str,
    badges_container: ui.element,
    table: ui.table,
) -> None:
    """Refresh owned jobs table and summary badges."""
    try:
        db_jobs = await _fetch_owned_jobs(current_user_id)
        jobs = [job_manager._db_model_to_job_info(model) for model in db_jobs]
        _render_badges(badges_container, jobs)
        table.rows = _owned_rows(job_manager, db_jobs)
        table.update()
    except Exception as exc:
        logger.error("Failed to load jobs: %s", exc, exc_info=True)


async def _refresh_shared_jobs(
    *,
    job_manager,
    current_user_id: str,
    current_user_is_admin: bool,
    table: ui.table,
) -> None:
    """Refresh shared jobs table."""
    try:
        shared_jobs = await _fetch_shared_jobs(current_user_id)
        table.rows = _shared_rows(job_manager, shared_jobs, current_user_is_admin)
        table.update()
    except Exception as exc:
        logger.error("Failed to load shared jobs: %s", exc, exc_info=True)


def _show_share_dialog(job_id: str) -> None:
    """Open share dialog."""
    dialog = render_share_dialog(job_id)
    dialog.open()


def _show_delete_dialog(
    *,
    job_id: str,
    job_manager,
    on_deleted: Callable[[], Awaitable[None] | None],
) -> None:
    """Open delete confirmation dialog and refresh table on success."""
    dialog = render_delete_dialog(job_id, job_manager, on_deleted=on_deleted)
    dialog.open()


@ui.page("/jobs")
@require_auth
def jobs_page():
    """Jobs list page."""
    from shandy import web_app

    job_manager = web_app.get_job_manager()
    is_configured, provider_name, config_errors = check_provider_config()
    _active_timers = setup_timer_cleanup()
    current_user_is_admin = is_current_user_admin()
    current_user_id = get_current_user_id()
    can_start_jobs = can_current_user_start_jobs()

    render_navigator(active_page="jobs", show_new_job=is_configured)
    if not is_configured:
        render_config_error_banner(provider_name, config_errors)
    if not can_start_jobs:
        render_pending_approval_notice()

    badges_container = ui.row().classes("w-full")

    with ui.tabs().classes("w-full") as tabs:
        my_jobs_tab = ui.tab("My Jobs", icon="work")
        shared_tab = ui.tab("Shared with me", icon="people")

    with ui.tab_panels(tabs, value=my_jobs_tab).classes("w-full"):
        with ui.tab_panel(my_jobs_tab):
            my_jobs_table = ui.table(
                columns=_OWNED_COLUMNS,
                rows=[],
                row_key="job_id",
                pagination=10,
            ).classes("w-full")
            my_jobs_table.add_slot("body-cell-job_id", render_job_id_slot())
            my_jobs_table.add_slot("body-cell-status", render_status_cell_slot())
            my_jobs_table.add_slot("body-cell-actions", render_actions_slot_with_delete())
            my_jobs_table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))
            my_jobs_table.on("share-job", lambda e: _show_share_dialog(e.args))

            async def refresh_my_jobs_table() -> None:
                await _refresh_owned_jobs(
                    job_manager=job_manager,
                    current_user_id=current_user_id,
                    badges_container=badges_container,
                    table=my_jobs_table,
                )

            my_jobs_table.on(
                "delete-job",
                lambda e: _show_delete_dialog(
                    job_id=e.args,
                    job_manager=job_manager,
                    on_deleted=refresh_my_jobs_table,
                ),
            )
            _active_timers.append(ui.timer(0.1, refresh_my_jobs_table, once=True))
            _active_timers.append(ui.timer(5.0, refresh_my_jobs_table))

        with ui.tab_panel(shared_tab):
            shared_jobs_table = ui.table(
                columns=_SHARED_COLUMNS,
                rows=[],
                row_key="job_id",
                pagination=10,
            ).classes("w-full")
            shared_jobs_table.add_slot("body-cell-job_id", render_job_id_slot())
            shared_jobs_table.add_slot("body-cell-status", render_status_cell_slot())
            shared_jobs_table.add_slot("body-cell-permission", render_permission_badge_slot())
            shared_jobs_table.add_slot("body-cell-actions", render_actions_slot_with_delete())
            shared_jobs_table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))

            async def refresh_shared_jobs_table() -> None:
                await _refresh_shared_jobs(
                    job_manager=job_manager,
                    current_user_id=current_user_id,
                    current_user_is_admin=current_user_is_admin,
                    table=shared_jobs_table,
                )

            shared_jobs_table.on(
                "delete-job",
                lambda e: _show_delete_dialog(
                    job_id=e.args,
                    job_manager=job_manager,
                    on_deleted=refresh_shared_jobs_table,
                ),
            )
            _active_timers.append(ui.timer(0.1, refresh_shared_jobs_table, once=True))
            _active_timers.append(ui.timer(5.0, refresh_shared_jobs_table))
