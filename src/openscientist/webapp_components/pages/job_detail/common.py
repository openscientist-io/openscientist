"""Shared context, knowledge-state loaders and the poll's snapshot for the job detail page."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from nicegui import ui
from sqlalchemy import select

from openscientist.database.models.job import Job as JobModel
from openscientist.database.models.job_data_file import JobDataFile
from openscientist.database.session import thread_safe_user_session
from openscientist.job.types import JobStatus, completed_iterations
from openscientist.job_files import list_job_files
from openscientist.knowledge_state import KnowledgeState
from openscientist.knowledge_state_db import (
    JobPageSnapshot,
    analysis_log_action_counts,
    job_page_snapshot,
)
from openscientist.webapp_components.pages.job_detail.prompt import Attachment, attachment_url

#: How many analysis log rows of each action type each iteration holds.
ActionCounts = dict[int, dict[str, int]]

logger = logging.getLogger(__name__)


def _report_tab_tooltip(status: JobStatus) -> str | None:
    """The tooltip of a disabled Report tab, or None when the tab is enabled.

    The report renders for completed jobs only, and a failed job has no
    report to show.
    """
    if status is JobStatus.COMPLETED:
        return None
    if status is JobStatus.FAILED:
        return "The job failed, so it has no report."
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
    """Load the knowledge state and the analysis log's action counts,
    returning (data, counts, error_message)."""
    try:
        async with thread_safe_user_session(UUID(user_id)) as session:
            ks = await KnowledgeState.load_from_database(job_id, session)
            counts = await analysis_log_action_counts(session, UUID(job_id))
        return ks.to_dict(), counts, None
    except Exception as e:
        logger.warning("Failed to load knowledge state from database for %s: %s", job_id, e)
        return None, {}, "Knowledge state is unavailable. Please refresh the page."


@dataclass(frozen=True)
class _PageSnapshot:
    """What the page compares across ticks: the database's job probe plus the
    job's plot files, which the database never sees."""

    job: JobPageSnapshot
    plot_names: tuple[str, ...]


async def _page_snapshot(
    job_id: str, user_id: str, job_dir: Path
) -> tuple[JobModel, _PageSnapshot] | None:
    """The page's per-tick change probe, read as the page's viewer, or None
    once the caller may no longer see the job."""
    async with thread_safe_user_session(UUID(user_id)) as session:
        probe = await job_page_snapshot(session, UUID(job_id))
    if probe is None:
        return None
    job_row, db_snapshot = probe
    plot_names = tuple(
        name for name in list_job_files(job_dir, "provenance") if name.endswith((".png", ".json"))
    )
    return job_row, _PageSnapshot(job=db_snapshot, plot_names=plot_names)


async def _load_attachments(job_id: str, user_id: str) -> list[Attachment]:
    """The job's attached files in upload order, read as the page's viewer."""
    async with thread_safe_user_session(UUID(user_id)) as session:
        rows = await session.execute(
            select(JobDataFile.filename, JobDataFile.file_path, JobDataFile.file_size)
            .where(JobDataFile.job_id == UUID(job_id))
            .order_by(JobDataFile.created_at, JobDataFile.id)
        )
    return [
        Attachment(name=name, url=attachment_url(job_id, path), size=size)
        for name, path, size in rows.all()
    ]


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
    is_owner: bool
    can_edit: bool
    owner_name: str | None
    job_dir: Path
    ks_data: dict[str, Any] | None
    action_counts: ActionCounts
    ks_load_error: str | None
    spend: float
    direct_access: bool
    state: dict[str, Any]
    share_dialog: Any
    delete_dialog: Any
    notifications_dialog: Any
    #: The attached files the viewer may open, none for a reader who sees the
    #: job only because it is public.
    attachments: list[Attachment]
