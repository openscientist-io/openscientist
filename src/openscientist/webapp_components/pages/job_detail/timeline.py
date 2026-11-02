"""Research Log tab: stats badges, timeline content and polling."""

from typing import Any

from nicegui import ui

from openscientist.agent.factory import agent_class_for_provider_id
from openscientist.job.repository import get_job
from openscientist.job.types import JobInfo, JobStatus
from openscientist.run_environment import run_environment_text
from openscientist.webapp_components.components.common import (
    STATUS_COLORS,
    render_stat_badges,
    render_thinking_status,
)
from openscientist.webapp_components.components.job_table import render_job_action_buttons
from openscientist.webapp_components.pages.job_detail.common import (
    _derive_progress_from_ks,
    _JobDetailContext,
    _load_knowledge_state,
)
from openscientist.webapp_components.pages.job_detail.feedback import _refresh_feedback_panel
from openscientist.webapp_components.pages.job_detail.iterations import (
    _render_timeline_content,
    _show_no_timeline_activity,
)
from openscientist.webapp_components.utils import (
    guard_client,
    is_client_connected,
)


def _provider_display_name(provider_id: str) -> str:
    """The provider's own display name, or a titled id for an unknown provider."""
    from openscientist.providers import provider_class

    try:
        return provider_class(provider_id).display_name
    except ValueError:
        return provider_id.title()


def _format_model_name(llm_model: str | None) -> str | None:
    """Map a raw model id to a human-readable name, or None when unset.

    Codex runs use the account/config default and store no model id, so the
    page shows a provider badge instead of a model badge in that case.
    """
    if not llm_model:
        return None

    model_lower = llm_model.lower()
    if "opus-4" in model_lower:
        return "Claude Opus 4"
    if "sonnet-4-5" in model_lower or "sonnet-4.5" in model_lower:
        return "Claude Sonnet 4.5"
    if "sonnet-4" in model_lower:
        return "Claude Sonnet 4"
    if "haiku-4" in model_lower:
        return "Claude Haiku 4"

    return llm_model


def _stats_badges(latest_job: Any, lit_count: int, hyp_count: int = 0) -> list[Any]:
    status_color = STATUS_COLORS.get(latest_job.status, "gray")
    badges = [("Status", latest_job.status.value.replace("_", " "), status_color)]
    queue_reason = getattr(latest_job, "queue_reason", None)
    if latest_job.status == JobStatus.QUEUED and queue_reason:
        badges.append(("Waiting on", queue_reason, "orange"))
    stop_note = getattr(latest_job, "stop_note", None)
    if latest_job.status == JobStatus.COMPLETED and stop_note:
        badges.append(("Stopped early", stop_note, "orange"))
    if latest_job.status not in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
        badges.append(
            (
                "Progress",
                f"{latest_job.iterations_completed}/{latest_job.max_iterations}",
                "blue",
            )
        )
    badges.extend(
        [("Findings", latest_job.findings_count, "green"), ("Papers", lit_count, "purple")]
    )
    if hyp_count:
        badges.append(("Hypotheses", hyp_count, "orange"))
    provider_id = getattr(latest_job, "llm_provider", None)
    if provider_id:
        agent_cls = agent_class_for_provider_id(provider_id)
        badges.append(("Agent", agent_cls.display_name, "indigo"))
        badges.append(("Provider", _provider_display_name(provider_id), "teal"))
    # Show the model as its own badge when known. This is independent of the
    # provider badge: the provider is where the model is hosted, the model is
    # which one ran. Codex on an account default records no model id, so the
    # model badge is simply omitted in that case.
    model_name = _format_model_name(getattr(latest_job, "llm_model", None))
    if model_name:
        badges.append(("Model", model_name, "cyan"))
    return badges


def _render_job_stats_content(context: _JobDetailContext) -> None:
    if not is_client_connected():
        return

    latest_job = context.job_info
    if latest_job is None:
        return

    latest_ks = context.ks_data
    lit_count = len(latest_ks.get("literature", [])) if latest_ks else 0
    hyp_count = len(latest_ks.get("hypotheses", [])) if latest_ks else 0
    render_stat_badges(_stats_badges(latest_job, lit_count, hyp_count))
    run_environment = run_environment_text(latest_job.version_info)
    if run_environment:
        ui.label(run_environment).classes("text-xs text-gray-500 mt-1")

    if latest_job.status == JobStatus.RUNNING and latest_ks:
        agent_status = latest_ks.get("agent_status")
        if agent_status:
            with ui.element("div").classes("mt-2"):
                render_thinking_status(agent_status)


def _render_research_question_card(context: _JobDetailContext) -> None:
    with ui.card().classes("w-full mb-4"), ui.row().classes("w-full items-start justify-between"):
        with ui.column().classes("flex-1"):
            ui.label("Research Question").classes("text-subtitle2 font-bold")
            ui.label(context.job_info.research_question).classes("text-lg")
            consensus = context.ks_data.get("consensus_answer") if context.ks_data else None
            if consensus and context.job_info.status == JobStatus.COMPLETED:
                with ui.element("div").classes(
                    "mt-3 p-3 bg-emerald-50 border-l-4 border-emerald-500 rounded"
                ):
                    ui.label("Consensus Answer").classes(
                        "text-xs font-bold text-emerald-700 uppercase tracking-wide"
                    )
                    ui.label(consensus).classes("text-emerald-900 mt-1")

        render_job_action_buttons(
            on_share=context.share_dialog.open if context.is_owner else None,
            on_delete=context.delete_dialog.open if context.is_owner else None,
            on_notifications=context.notifications_dialog.open,
        )


def _render_timeline_content_for_context(context: _JobDetailContext) -> None:
    if not is_client_connected():
        return

    timeline_ks = context.ks_data
    latest_job = context.job_info
    if not timeline_ks or not latest_job:
        _show_no_timeline_activity()
        return

    _render_timeline_content(
        timeline_ks=timeline_ks,
        latest_job=latest_job,
        job_dir=context.job_dir,
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


async def _check_and_refresh(
    context: _JobDetailContext,
    render_job_stats: Any,
    render_timeline: Any,
    stats_timer_holder: dict[str, Any],
) -> None:
    db_job = await get_job(context.job_id)
    if db_job is None:
        _handle_missing_job_during_poll(stats_timer_holder)
        return

    latest_ks, _ = await _load_knowledge_state(context.job_id, context.user_id)

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


async def _render_timeline_tab(context: _JobDetailContext) -> None:
    @ui.refreshable
    def render_job_stats() -> None:
        _render_job_stats_content(context)

    @ui.refreshable
    def render_timeline() -> None:
        _render_timeline_content_for_context(context)

    render_job_stats()
    _render_research_question_card(context)
    ui.label("Investigation Timeline").classes("text-h6 font-bold mb-2")
    render_timeline()

    feedback_container = ui.column().classes("w-full")
    await _refresh_feedback_panel(
        feedback_container=feedback_container,
        job_manager=context.job_manager,
        job_id=context.job_id,
        user_id=context.user_id,
        can_edit=context.can_edit,
        job_dir=context.job_dir,
        active_timers=context.active_timers,
        ks_data=context.ks_data,
    )

    stats_timer_holder: dict[str, Any] = {"timer": None}

    @guard_client
    async def check_and_refresh() -> None:
        await _check_and_refresh(context, render_job_stats, render_timeline, stats_timer_holder)

    if context.job_info.status in _polling_statuses():
        stats_timer_holder["timer"] = ui.timer(2.0, check_and_refresh)
        context.active_timers.append(stats_timer_holder["timer"])
