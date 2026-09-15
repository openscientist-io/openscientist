"""Polling, state-refresh, and automatic page update logic for the job detail
page.

Periodically re-checks the job's database row and knowledge state, decides
whether the stats/timeline refreshables need to re-render, and navigates to a
fresh page load when the job transitions into a state that requires it (e.g.
completed, failed, cancelled, awaiting feedback). Consumed by
`_render_timeline_tab` in `job_detail.py`, which remains the orchestration
seam between timeline, feedback, and polling.
"""

from typing import Any

from nicegui import ui

from openscientist.async_tasks import run_sync
from openscientist.job.types import JobInfo, JobStatus
from openscientist.job_manager import _db_get_job
from openscientist.webapp_components.pages.job_detail_context import (
    _derive_progress_from_ks,
    _JobDetailContext,
    _load_knowledge_state,
)


def _state_snapshot(latest_job: Any, latest_ks: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "findings_count": latest_job.findings_count,
        "papers_count": len(latest_ks.get("literature", [])) if latest_ks else 0,
        "iteration": latest_ks.get("iteration", 0) if latest_ks else 0,
        "log_entries": len(latest_ks.get("analysis_log", [])) if latest_ks else 0,
        "agent_status": latest_ks.get("agent_status") if latest_ks else None,
    }


def _stats_changed(state: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    return bool(
        state["findings_count"] != snapshot["findings_count"]
        or state["papers_count"] != snapshot["papers_count"]
        or state["iteration"] != snapshot["iteration"]
        or state["agent_status"] != snapshot["agent_status"]
    )


def _update_state_fields(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    state["findings_count"] = snapshot["findings_count"]
    state["papers_count"] = snapshot["papers_count"]
    state["iteration"] = snapshot["iteration"]
    state["agent_status"] = snapshot["agent_status"]


def _reload_required_statuses() -> list[JobStatus]:
    return [
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.AWAITING_FEEDBACK,
    ]


def _polling_statuses() -> list[JobStatus]:
    return [
        JobStatus.PENDING,
        JobStatus.RUNNING,
        JobStatus.QUEUED,
        JobStatus.AWAITING_FEEDBACK,
        JobStatus.GENERATING_REPORT,
    ]


def _handle_missing_job_during_poll(stats_timer_holder: dict[str, Any]) -> None:
    timer = stats_timer_holder.get("timer")
    if timer:
        timer.deactivate()


def _handle_status_transition(
    context: _JobDetailContext,
    latest_job: Any,
    stats_timer_holder: dict[str, Any],
    render_job_stats: Any,
) -> None:
    if latest_job.status == context.state["status"]:
        return
    context.state["status"] = latest_job.status
    if latest_job.status in _reload_required_statuses():
        _handle_missing_job_during_poll(stats_timer_holder)
        ui.navigate.to(f"/job/{context.job_id}")
        return
    render_job_stats.refresh()


def _check_and_refresh(
    context: _JobDetailContext,
    render_job_stats: Any,
    render_timeline: Any,
    stats_timer_holder: dict[str, Any],
) -> None:
    db_job = run_sync(_db_get_job(context.job_id))
    if db_job is None:
        _handle_missing_job_during_poll(stats_timer_holder)
        return

    latest_ks, _ = _load_knowledge_state(context.job_id, context.user_id)

    iters, findings = _derive_progress_from_ks(latest_ks, db_job.status, db_job.current_iteration)
    latest_job = JobInfo.from_db_model(db_job, iters, findings)

    snapshot = _state_snapshot(latest_job, latest_ks)

    # Update context before calling .refresh() so refreshables read fresh data
    context.ks_data = latest_ks
    context.job_info = latest_job

    if _stats_changed(context.state, snapshot):
        _update_state_fields(context.state, snapshot)
        render_job_stats.refresh()

    if snapshot["log_entries"] > context.state["log_entries"]:
        context.state["log_entries"] = snapshot["log_entries"]
        render_timeline.refresh()

    _handle_status_transition(context, latest_job, stats_timer_holder, render_job_stats)
