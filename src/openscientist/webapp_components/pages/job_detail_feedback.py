"""Feedback submission, feedback-panel rendering, and countdown behavior for
the job detail page.

Renders the "awaiting feedback" panel shown between iterations: lets an
editor submit guidance (or continue without it) and shows a live countdown
to the auto-continue timeout for view-only users. Consumed by
`_render_timeline_tab` in `job_detail.py`, which remains the orchestration
seam between timeline, feedback, and polling.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from nicegui import ui

from openscientist.async_tasks import run_sync
from openscientist.job.types import JobStatus
from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import update_job_status
from openscientist.webapp_components.utils import guard_client


def _next_iteration_for_feedback(ks_data: dict[str, Any] | None) -> int:
    if ks_data is None:
        return 1
    return int(ks_data.get("iteration", 1))


def _submit_feedback_and_continue(
    job_dir: Path, job_id: str, user_id: str, completed_iter: int, feedback_text: str
) -> None:
    ks = KnowledgeState.load_from_database_sync(job_id, UUID(user_id))
    if feedback_text.strip():
        ks.add_feedback(feedback_text.strip(), completed_iter)
        ks.save_to_database_sync(job_id, UUID(user_id))

    try:
        run_sync(update_job_status(job_dir, "running"))
    except Exception:
        ui.notify("Failed to continue job. Please try again.", type="negative")
        return
    ui.notify("Continuing to next iteration", type="positive")
    ui.navigate.to(f"/job/{job_id}")


def _parse_awaiting_started_at(awaiting_since: str | None) -> datetime | None:
    if not awaiting_since:
        return None
    try:
        started = datetime.fromisoformat(awaiting_since)
    except (ValueError, TypeError):
        return None
    if started.tzinfo is None:
        return started.replace(tzinfo=UTC)
    return started


def _render_feedback_countdown(awaiting_since: str | None, active_timers: list[Any]) -> None:
    started = _parse_awaiting_started_at(awaiting_since)
    if started is None:
        ui.label("Auto-continues after 15 minutes if no response.").classes(
            "text-xs text-gray-500 mt-2"
        )
        return

    timeout_minutes = 15
    countdown_label = ui.label("").classes("text-xs text-gray-500 mt-2")
    timer_ref: list[Any] = [None]

    @guard_client
    def update_countdown() -> None:
        now = datetime.now(UTC)
        elapsed = (now - started).total_seconds()
        remaining = (timeout_minutes * 60) - elapsed
        if remaining <= 0:
            countdown_label.text = "Auto-continuing now..."
            if timer_ref[0]:
                timer_ref[0].deactivate()
            return
        mins = int(remaining // 60)
        secs = int(remaining % 60)
        countdown_label.text = f"Auto-continues in {mins}:{secs:02d} if no response."

    update_countdown()
    timer_ref[0] = ui.timer(1.0, update_countdown)
    active_timers.append(timer_ref[0])


def _render_feedback_panel(
    feedback_container: ui.column,
    latest_job: Any,
    can_edit: bool,
    job_dir: Path,
    job_id: str,
    user_id: str,
    active_timers: list[Any],
    ks_data: dict[str, Any] | None = None,
) -> None:
    if latest_job.status != JobStatus.AWAITING_FEEDBACK:
        return

    next_iter = _next_iteration_for_feedback(ks_data)
    completed_iter = next_iter - 1 if next_iter > 1 else 1
    awaiting_since = latest_job.started_at

    with (
        feedback_container,
        ui.card().classes("w-full mt-2 bg-yellow-50 border-2 border-yellow-400 p-6"),
    ):
        ui.label(f"Iteration {completed_iter} Complete - Awaiting Your Input").classes(
            "text-h6 font-bold text-yellow-800"
        )
        if can_edit:
            ui.label(
                "Provide guidance for the next iteration, or continue without feedback."
            ).classes("text-sm text-gray-700 mb-4")
            feedback_input = ui.textarea(
                label="Your Feedback (optional)",
                placeholder="e.g., Focus on metabolic pathways, or investigate the correlation with gene X...",
            ).classes("w-full")

            def submit_feedback(fi: Any = feedback_input, ci: int = completed_iter) -> None:
                _submit_feedback_and_continue(job_dir, job_id, user_id, ci, fi.value)

            with ui.row().classes("w-full gap-2 mt-2"):
                ui.button(
                    "Submit & Continue",
                    on_click=submit_feedback,
                    icon="send",
                ).props("color=primary")
                ui.button(
                    "Continue Without Feedback",
                    on_click=submit_feedback,
                    icon="arrow_forward",
                ).props("color=secondary outline")
            return

        ui.label("You have view-only access to this job.").classes(
            "text-sm text-gray-500 italic mb-4"
        )
        _render_feedback_countdown(awaiting_since, active_timers)


def _refresh_feedback_panel(
    feedback_container: ui.column,
    job_manager: Any,
    job_id: str,
    user_id: str,
    can_edit: bool,
    job_dir: Path,
    active_timers: list[Any],
    ks_data: dict[str, Any] | None = None,
) -> None:
    feedback_container.clear()
    latest_job = job_manager.get_job(job_id)
    if latest_job is None:
        return
    _render_feedback_panel(
        feedback_container=feedback_container,
        latest_job=latest_job,
        can_edit=can_edit,
        job_dir=job_dir,
        job_id=job_id,
        user_id=user_id,
        active_timers=active_timers,
        ks_data=ks_data,
    )
