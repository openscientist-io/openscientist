"""Research Log tab: stats badges, timeline content and polling."""

from typing import Any

from nicegui import ui
from nicegui.functions.refreshable import refreshable

from openscientist.agent.factory import agent_class_for_provider_id
from openscientist.budget import format_spend
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
    _page_snapshot,
    _PageSnapshot,
    _render_access_lost,
)
from openscientist.webapp_components.pages.job_detail.feedback import _refresh_feedback_panel
from openscientist.webapp_components.pages.job_detail.iterations import _Timeline
from openscientist.webapp_components.pages.job_detail.prompt import render_research_prompt
from openscientist.webapp_components.utils import (
    is_client_connected,
    session_checked_timer,
)

#: The stats card's rendered values, its badges and the live status line.
_StatsModel = tuple[tuple[tuple[str, str | int, str], ...], str | None]


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


def _stats_badges(
    latest_job: Any, lit_count: int, hyp_count: int = 0, spend: float | None = None
) -> list[tuple[str, str | int, str]]:
    status_color = STATUS_COLORS.get(latest_job.status, "gray")
    badges: list[tuple[str, str | int, str]] = [
        ("Status", latest_job.status.value.replace("_", " "), status_color)
    ]
    queue_reason = getattr(latest_job, "queue_reason", None)
    if latest_job.status == JobStatus.QUEUED and queue_reason:
        badges.append(("Waiting on", queue_reason, "orange"))
    stop_note = getattr(latest_job, "stop_note", None)
    if latest_job.status == JobStatus.COMPLETED and stop_note:
        badges.append(("Stopped early", stop_note, "orange"))
    if getattr(latest_job, "is_public", False):
        badges.append(("Public", "anyone signed in", "teal"))
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
    if spend is not None:
        badges.append(("Spend", format_spend(spend), "green"))
    return badges


def _status_line_for(latest_job: Any, latest_ks: dict[str, Any] | None) -> str | None:
    """The live status line under the badges, or None when nothing is live.

    The report phase names itself, because a regenerating job carries no
    fresh agent status.
    """
    if latest_job.status == JobStatus.GENERATING_REPORT:
        return "Generating report..."
    if latest_job.status == JobStatus.RUNNING and latest_ks:
        return latest_ks.get("agent_status")
    return None


def _render_job_stats_content(context: _JobDetailContext) -> None:
    if not is_client_connected():
        return

    latest_job = context.job_info
    latest_ks = context.ks_data
    lit_count = len(latest_ks.get("literature", [])) if latest_ks else 0
    hyp_count = len(latest_ks.get("hypotheses", [])) if latest_ks else 0
    badges_row = render_stat_badges(
        _stats_badges(
            latest_job, lit_count, hyp_count, context.spend if context.spend_visible else None
        )
    )
    run_environment = run_environment_text(latest_job.version_info)
    if run_environment:
        with badges_row:
            with ui.icon("info").props("size=sm").classes("text-gray-400 align-middle"):
                ui.tooltip(run_environment).classes("os-env-tooltip")
    if context.owner_name:
        ui.label(f"Owner: {context.owner_name}").classes("text-xs text-gray-500 mt-1")

    status_line = _status_line_for(latest_job, latest_ks)
    if status_line:
        with ui.element("div").classes("mt-2"):
            render_thinking_status(status_line)


def _open_dialog(dialog: ui.dialog) -> None:
    dialog.open()


def _render_research_question_card(
    context: _JobDetailContext, render_prompt: refreshable[[], None]
) -> None:
    with ui.card().classes("w-full mb-4"), ui.row().classes("w-full items-start justify-between"):
        with ui.column().classes("flex-1 min-w-0"):
            ui.label("Research Question").classes("text-subtitle2 font-bold")
            render_prompt()
            consensus = context.ks_data.get("consensus_answer") if context.ks_data else None
            if consensus and context.job_info.status == JobStatus.COMPLETED:
                with ui.element("div").classes(
                    "mt-3 p-3 bg-emerald-50 border-l-4 border-emerald-500 rounded"
                ):
                    ui.label("Consensus Answer").classes(
                        "text-xs font-bold text-emerald-700 uppercase tracking-wide"
                    )
                    ui.label(consensus).classes("text-emerald-900 mt-1")

        on_share = (
            (lambda: _open_dialog(context.share_dialog))
            if context.is_owner and context.share_dialog is not None
            else None
        )
        on_delete = (lambda: _open_dialog(context.delete_dialog)) if context.is_owner else None
        render_job_action_buttons(
            on_share=on_share,
            on_delete=on_delete,
            on_notifications=lambda: _open_dialog(context.notifications_dialog),
        )


def _stats_model(
    latest_job: JobInfo, latest_ks: dict[str, Any] | None, spend: float | None
) -> _StatsModel:
    """The stats card's rendered values, compared across ticks so the card
    refreshes when a badge or the status line would actually change."""
    lit_count = len(latest_ks.get("literature", [])) if latest_ks else 0
    hyp_count = len(latest_ks.get("hypotheses", [])) if latest_ks else 0
    return tuple(_stats_badges(latest_job, lit_count, hyp_count, spend)), _status_line_for(
        latest_job, latest_ks
    )


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


def _stop_status_poll(stats_timer_holder: dict[str, ui.timer | None]) -> None:
    """Deactivate the status poll's timer if it is running."""
    timer = stats_timer_holder.get("timer")
    if timer:
        timer.deactivate()


def _handle_access_lost_during_poll(
    context: _JobDetailContext, stats_timer_holder: dict[str, ui.timer | None]
) -> None:
    """Stop the poll and replace the page with the access-lost notice."""
    _stop_status_poll(stats_timer_holder)
    if not is_client_connected():
        return
    ui.context.client.content.clear()
    _render_access_lost()


async def _check_and_refresh(
    context: _JobDetailContext,
    render_job_stats: refreshable[[], None],
    render_prompt: refreshable[[], None],
    timeline: _Timeline,
    stats_timer_holder: dict[str, ui.timer | None],
) -> None:
    probe = await _page_snapshot(context.job_id, context.user_id, context.job_dir)
    if probe is None:
        _handle_access_lost_during_poll(context, stats_timer_holder)
        return
    db_job, snapshot = probe

    old_snapshot: _PageSnapshot | None = context.state["snapshot"]
    if snapshot == old_snapshot:
        return

    if snapshot.job.status in _reload_required_statuses():
        context.state["status"] = snapshot.job.status
        _stop_status_poll(stats_timer_holder)
        ui.navigate.to(f"/job/{context.job_id}")
        return

    if snapshot.job.status != context.state["status"]:
        context.state["status"] = snapshot.job.status

    latest_ks, action_counts, _ = await _load_knowledge_state(context.job_id, context.user_id)
    if latest_ks is None:
        return

    iters, findings = _derive_progress_from_ks(
        latest_ks, snapshot.job.status, snapshot.job.current_iteration
    )
    latest_job = JobInfo.from_db_model(db_job, iters, findings)

    prompt_changed = latest_job.formatted_prompt != context.job_info.formatted_prompt

    # Update context before calling .refresh() so refreshables read fresh data
    context.ks_data = latest_ks
    context.action_counts = action_counts
    context.job_info = latest_job
    context.spend = snapshot.job.spend

    stats_model = _stats_model(
        latest_job, latest_ks, context.spend if context.spend_visible else None
    )
    if stats_model != context.state["stats_model"]:
        context.state["stats_model"] = stats_model
        render_job_stats.refresh()
    if prompt_changed:
        render_prompt.refresh()

    await timeline.update(old_snapshot, snapshot)
    context.state["snapshot"] = snapshot


async def _render_timeline_tab(context: _JobDetailContext) -> None:
    @ui.refreshable
    def render_job_stats() -> None:
        _render_job_stats_content(context)

    @ui.refreshable
    def render_prompt() -> None:
        render_research_prompt(
            context.job_info.research_question,
            context.job_info.formatted_prompt,
            context.attachments,
        )

    render_job_stats()
    _render_research_question_card(context, render_prompt)
    ui.label("Investigation Timeline").classes("text-h6 font-bold mb-2")
    timeline = _Timeline(context)
    timeline.build_initial()

    feedback_container = ui.column().classes("w-full")
    await _refresh_feedback_panel(
        feedback_container=feedback_container,
        job_manager=context.job_manager,
        job_id=context.job_id,
        user_id=context.user_id,
        can_edit=context.can_edit,
        job_dir=context.job_dir,
        ks_data=context.ks_data,
    )

    stats_timer_holder: dict[str, ui.timer | None] = {"timer": None}

    async def check_and_refresh() -> None:
        await _check_and_refresh(
            context, render_job_stats, render_prompt, timeline, stats_timer_holder
        )

    if context.job_info.status in _polling_statuses():
        stats_timer_holder["timer"] = session_checked_timer(2.0, check_and_refresh, context.user_id)
