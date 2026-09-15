"""Context loading, permissions, and initial page state for the job detail page.

Builds the `_JobDetailContext` used across the job detail page's tabs: resolves
job access/permissions, loads knowledge-state data, derives progress counters,
creates the page-level dialogs (share/delete/notifications), and renders the
job-level status notices (cancelled/knowledge-state-loading/failed).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from nicegui import ui

from openscientist.async_tasks import run_sync
from openscientist.auth import get_current_user_id
from openscientist.job.types import JobInfo, JobStatus
from openscientist.job_manager import _db_get_job, _db_get_share_permission
from openscientist.knowledge_state import KnowledgeState
from openscientist.webapp_components.error_handler import get_user_friendly_error
from openscientist.webapp_components.ui_components import (
    render_delete_dialog,
    render_error_card,
    render_notifications_dialog,
    render_share_dialog,
)
from openscientist.webapp_components.utils import setup_timer_cleanup

logger = logging.getLogger(__name__)


def _derive_progress_from_ks(
    ks_data: dict[str, Any] | None,
    status: str,
    default_iterations: int,
) -> tuple[int, int]:
    """Derive iteration/findings counts from already-loaded KS data."""
    if ks_data is None:
        return default_iterations, 0
    findings_count = len(ks_data.get("findings", []))
    ks_iteration = int(ks_data.get("iteration", 1))
    if status in ("running", "awaiting_feedback"):
        iterations_completed = ks_iteration - 1 if ks_iteration > 1 else 0
    else:
        iterations_completed = ks_iteration
    return iterations_completed, findings_count


def _load_knowledge_state(job_id: str, user_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Load knowledge state from database, returning (data, error_message)."""
    try:
        ks = KnowledgeState.load_from_database_sync(job_id, UUID(user_id))
        return ks.to_dict(), None
    except Exception as e:
        logger.warning("Failed to load knowledge state from database for %s: %s", job_id, e)
        return None, "Knowledge state is unavailable. Please refresh the page."


@dataclass
class _JobDetailContext:
    job_id: str
    user_id: str
    job_manager: Any
    job_info: Any
    db_job: Any
    is_owner: bool
    can_edit: bool
    job_dir: Path
    ks_data: dict[str, Any] | None
    ks_load_error: str | None
    state: dict[str, Any]
    active_timers: list[Any]
    share_dialog: Any
    delete_dialog: Any
    notifications_dialog: Any


def _render_job_not_found() -> None:
    ui.label("Job not found").classes("text-h5")
    ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"))


def _load_db_job_for_user(job_id: str, user_id: str) -> Any:
    try:
        return run_sync(_db_get_job(job_id, user_id=UUID(user_id)))
    except ValueError:
        return None
    except Exception:
        logger.error(
            "Failed to check job access: job_id=%s user_id=%s",
            job_id,
            user_id,
            exc_info=True,
        )
        return None


def _resolve_job_permissions(job_id: str, user_id: str, db_job: Any) -> tuple[bool, bool]:
    is_owner = db_job.owner_id == UUID(user_id)
    if is_owner:
        return True, True
    share_permission = run_sync(_db_get_share_permission(job_id, UUID(user_id)))
    return False, share_permission == "edit"


def _job_page_title(job_info: Any) -> str:
    page_title: str = job_info.short_title or job_info.research_question[:50]
    if len(job_info.research_question) > 50 and not job_info.short_title:
        page_title += "..."
    return page_title


def _initial_job_state(job_info: Any, ks_data: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "status": job_info.status,
        "iteration": ks_data.get("iteration", 0) if ks_data else 0,
        "findings_count": job_info.findings_count,
        "papers_count": len(ks_data.get("literature", [])) if ks_data else 0,
        "log_entries": len(ks_data.get("analysis_log", [])) if ks_data else 0,
        "agent_status": ks_data.get("agent_status") if ks_data else None,
    }


def _create_page_dialogs(job_id: str, job_manager: Any, user_id: str) -> tuple[Any, Any, Any]:
    share_dialog = render_share_dialog(job_id)
    delete_dialog = render_delete_dialog(
        job_id,
        job_manager,
        on_deleted=lambda: ui.navigate.to("/jobs"),
    )
    notifications_dialog = render_notifications_dialog(job_id, user_id)
    return share_dialog, delete_dialog, notifications_dialog


def _build_job_detail_context(job_id: str) -> _JobDetailContext | None:
    from openscientist import web_app

    job_manager = web_app.get_job_manager()
    user_id = get_current_user_id()
    assert user_id is not None
    db_job = _load_db_job_for_user(job_id, user_id)
    if db_job is None:
        return None

    is_owner, can_edit = _resolve_job_permissions(job_id, user_id, db_job)

    job_dir = job_manager.jobs_dir / job_id
    ks_data, ks_load_error = _load_knowledge_state(job_id, user_id)

    # Derive progress from already-loaded KS data instead of loading it again via get_job()
    iterations_completed, findings_count = _derive_progress_from_ks(
        ks_data, db_job.status, db_job.current_iteration
    )
    job_info = JobInfo.from_db_model(db_job, iterations_completed, findings_count)
    active_timers = setup_timer_cleanup()
    share_dialog, delete_dialog, notifications_dialog = _create_page_dialogs(
        job_id,
        job_manager,
        user_id,
    )

    return _JobDetailContext(
        job_id=job_id,
        user_id=user_id,
        job_manager=job_manager,
        job_info=job_info,
        db_job=db_job,
        is_owner=is_owner,
        can_edit=can_edit,
        job_dir=job_dir,
        ks_data=ks_data,
        ks_load_error=ks_load_error,
        state=_initial_job_state(job_info, ks_data),
        active_timers=active_timers,
        share_dialog=share_dialog,
        delete_dialog=delete_dialog,
        notifications_dialog=notifications_dialog,
    )


def _render_cancelled_notice(job_info: Any) -> None:
    with ui.card().classes("w-full bg-orange-50 border border-orange-300 mb-4 p-4"):
        ui.label("Job Cancelled").classes("text-subtitle2 font-bold text-orange-800")
        ui.label(job_info.cancellation_reason or "No reason provided").classes("text-orange-700")


def _render_ks_loading_notice(ks_load_error: str) -> None:
    with ui.card().classes("w-full bg-yellow-50 border border-yellow-300 mb-4 p-4"):
        ui.label("Loading...").classes("text-subtitle2 font-bold text-yellow-800")
        ui.label(ks_load_error).classes("text-yellow-700")


def _render_job_status_notices(context: _JobDetailContext) -> None:
    if context.job_info.status == JobStatus.FAILED and context.job_info.error:
        error_info = get_user_friendly_error(context.job_info.error)
        render_error_card(error_info, context.job_info, context.job_dir)
    if context.job_info.status == JobStatus.CANCELLED:
        _render_cancelled_notice(context.job_info)
    if context.ks_load_error:
        _render_ks_loading_notice(context.ks_load_error)
