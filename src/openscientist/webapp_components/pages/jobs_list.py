"""Jobs list page."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from nicegui import ui
from sqlalchemy import func, select

from openscientist.auth import (
    can_current_user_start_jobs,
    get_current_user_id,
    is_current_user_admin,
    require_auth,
)
from openscientist.database.models import Job, JobShare, User
from openscientist.database.models.finding import Finding
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_admin_session, get_session_ctx
from openscientist.job.types import JobStatus
from openscientist.providers import check_provider_config
from openscientist.webapp_components.ui_components import (
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
from openscientist.webapp_components.utils import setup_timer_cleanup

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


def _derive_progress_from_db(status: str, current_iteration: int) -> int:
    """Derive iterations_completed from Job model columns (no KS load needed)."""
    if status in ("running", "awaiting_feedback"):
        return current_iteration - 1 if current_iteration > 1 else 0
    return current_iteration


async def _batch_findings_counts(session: Any, job_ids: list[UUID]) -> dict[UUID, int]:
    """Batch-fetch findings counts for multiple jobs in one query."""
    if not job_ids:
        return {}
    stmt = (
        select(Finding.job_id, func.count())
        .where(Finding.job_id.in_(job_ids))
        .group_by(Finding.job_id)
    )
    result = await session.execute(stmt)
    return dict(result.all())


def _render_badges(badges_container: ui.element, jobs: list[Job]) -> None:
    """Render aggregate job counters from raw DB models."""
    status_counts = {
        status.value: sum(1 for job in jobs if job.status == status.value) for status in JobStatus
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


def _owned_rows(db_jobs: list[Job], findings_counts: dict[UUID, int]) -> list[dict[str, Any]]:
    """Build owned-jobs table rows."""
    return [
        {
            "job_id": str(job.id),
            "question": _truncate_question(job.title),
            "status": job.status,
            "error": job.error_message or "",
            "iterations": f"{_derive_progress_from_db(job.status, job.current_iteration)}/{job.max_iterations}",
            "findings": findings_counts.get(job.id, 0),
            "created": job.created_at.isoformat()[:19],
            "can_share": True,
            "can_delete": True,
        }
        for job in db_jobs
    ]


def _shared_rows(
    shared_jobs: list[tuple[Job, User, JobShare]],
    findings_counts: dict[UUID, int],
    current_user_is_admin: bool,
) -> list[dict[str, Any]]:
    """Build shared-jobs table rows."""
    rows: list[dict[str, Any]] = []
    for job, owner, share in shared_jobs:
        rows.append(
            {
                "job_id": str(job.id),
                "question": _truncate_question(job.title),
                "owner": owner.name,
                "permission": share.permission_level,
                "status": job.status,
                "error": job.error_message or "",
                "iterations": f"{_derive_progress_from_db(job.status, job.current_iteration)}/{job.max_iterations}",
                "findings": findings_counts.get(job.id, 0),
                "created": job.created_at.isoformat()[:19],
                "can_share": False,
                "can_delete": current_user_is_admin,
            }
        )
    return rows


async def _refresh_owned_jobs(
    *,
    current_user_id: str,
    badges_container: ui.element,
    table: ui.table,
) -> None:
    """Refresh owned jobs table and summary badges."""
    try:
        db_jobs = await _fetch_owned_jobs(current_user_id)
        _render_badges(badges_container, db_jobs)
        async with get_session_ctx() as session:
            user_uuid = UUID(current_user_id)
            await set_current_user(session, user_uuid)
            findings_counts = await _batch_findings_counts(session, [j.id for j in db_jobs])
        table.rows = _owned_rows(db_jobs, findings_counts)
        table.update()
    except Exception as exc:
        logger.error("Failed to load jobs: %s", exc, exc_info=True)


async def _refresh_shared_jobs(
    *,
    current_user_id: str,
    current_user_is_admin: bool,
    table: ui.table,
) -> None:
    """Refresh shared jobs table."""
    try:
        shared_jobs = await _fetch_shared_jobs(current_user_id)
        job_ids = [job.id for job, _, _ in shared_jobs]
        async with get_admin_session() as session:
            findings_counts = await _batch_findings_counts(session, job_ids)
        table.rows = _shared_rows(shared_jobs, findings_counts, current_user_is_admin)
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
    job_manager: Any,
    on_deleted: Callable[[], Awaitable[None] | None],
) -> None:
    """Open delete confirmation dialog and refresh table on success."""
    dialog = render_delete_dialog(job_id, job_manager, on_deleted=on_deleted)
    dialog.open()


@ui.page("/jobs")
@require_auth
def jobs_page() -> None:
    """Jobs list page."""
    from openscientist import web_app

    job_manager = web_app.get_job_manager()
    is_configured, provider_name, config_errors = check_provider_config()
    _active_timers = setup_timer_cleanup()
    current_user_is_admin = is_current_user_admin()
    current_user_id = get_current_user_id()
    assert current_user_id is not None
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
