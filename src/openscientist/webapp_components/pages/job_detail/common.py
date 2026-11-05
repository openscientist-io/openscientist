"""Shared context and knowledge-state loaders for the job detail page."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from nicegui import ui

from openscientist.database.session import thread_safe_user_session
from openscientist.job.types import JobStatus, completed_iterations
from openscientist.knowledge_state_db import analysis_log_action_counts, load_without_analysis_log

#: How many analysis log rows of each action type each iteration holds.
ActionCounts = dict[int, dict[str, int]]


def _logged_actions(counts: ActionCounts) -> int:
    """How many analysis log rows the whole job holds."""
    return sum(sum(by_action.values()) for by_action in counts.values())


logger = logging.getLogger(__name__)


def _report_tab_tooltip(status: JobStatus) -> str | None:
    """The tooltip of a disabled Report tab, or None when the tab is enabled.

    The report renders for finished jobs; a failed job may still have written
    a partial final report.
    """
    if status in (JobStatus.COMPLETED, JobStatus.FAILED):
        return None
    return "The report is available once the job finishes."


def _chat_tab_tooltip(status: JobStatus) -> str | None:
    """The tooltip of a disabled Chat tab, or None when the tab is enabled.

    The assistant answers from the findings, which exist only after the job
    has finished.
    """
    if status is JobStatus.COMPLETED:
        return None
    return "Chat opens once the job finishes."


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
    return completed_iterations(status, ks_iteration), findings_count


async def _load_knowledge_state(
    job_id: str, user_id: str
) -> tuple[dict[str, Any] | None, ActionCounts, str | None]:
    """Load the knowledge state without its analysis log rows, and the log's
    action counts, returning (data, counts, error_message)."""
    try:
        async with thread_safe_user_session(UUID(user_id)) as session:
            ks = await load_without_analysis_log(session, job_id)
            counts = await analysis_log_action_counts(session, UUID(job_id))
        return ks.to_dict(), counts, None
    except Exception as e:
        logger.warning("Failed to load knowledge state from database for %s: %s", job_id, e)
        return None, {}, "Knowledge state is unavailable. Please refresh the page."


def _render_access_lost() -> None:
    """The page body once the caller can no longer view the job."""
    with ui.column().classes("w-full items-center mt-16 gap-4"):
        ui.label("Access to this job has been revoked.").classes("text-h5")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"))


@dataclass
class _JobDetailContext:
    job_id: str
    user_id: str
    job_manager: Any
    job_info: Any
    db_job: Any
    is_owner: bool
    can_edit: bool
    owner_name: str | None
    job_dir: Path
    ks_data: dict[str, Any] | None
    action_counts: ActionCounts
    ks_load_error: str | None
    state: dict[str, Any]
    share_dialog: Any
    delete_dialog: Any
    notifications_dialog: Any
