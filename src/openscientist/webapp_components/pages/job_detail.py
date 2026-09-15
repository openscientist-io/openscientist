"""Job detail page with progressive disclosure UI.

Uses NiceGUI's websocket-based updates for real-time UI changes.
The page uses @ui.refreshable decorators to enable in-place updates
without full page reloads.
"""

from typing import Any

from nicegui import ui

from openscientist.auth import require_auth
from openscientist.webapp_components.pages.job_detail_chat import _render_chat_tab
from openscientist.webapp_components.pages.job_detail_context import (
    _build_job_detail_context,
    _job_page_title,
    _JobDetailContext,
    _render_job_not_found,
    _render_job_status_notices,
)
from openscientist.webapp_components.pages.job_detail_feedback import _refresh_feedback_panel
from openscientist.webapp_components.pages.job_detail_polling import (
    _check_and_refresh,
    _polling_statuses,
)
from openscientist.webapp_components.pages.job_detail_report import _render_report_tab

# _analysis_log_meta_lines, _collect_iteration_plots, _stats_badges, and
# _timeline_header_text are re-exported below for backward compatibility:
# existing tests import them from this module rather than from
# job_detail_timeline, so they must remain importable from here unchanged.
from openscientist.webapp_components.pages.job_detail_timeline import (
    _analysis_log_meta_lines,  # noqa: F401
    _collect_iteration_plots,  # noqa: F401
    _render_job_stats_content,
    _render_research_question_card,
    _render_timeline_content_for_context,
    _stats_badges,  # noqa: F401
    _timeline_header_text,  # noqa: F401
)
from openscientist.webapp_components.ui_components import render_navigator
from openscientist.webapp_components.utils import guard_client


def _render_timeline_tab(context: _JobDetailContext) -> None:
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
    _refresh_feedback_panel(
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
    def check_and_refresh() -> None:
        _check_and_refresh(context, render_job_stats, render_timeline, stats_timer_holder)

    if context.job_info.status in _polling_statuses():
        stats_timer_holder["timer"] = ui.timer(2.0, check_and_refresh)
        context.active_timers.append(stats_timer_holder["timer"])


def _render_job_tabs(context: _JobDetailContext) -> None:
    with ui.tabs().classes("w-full") as tabs:
        timeline_tab = ui.tab("Research Log")
        report_tab = ui.tab("Report")
        chat_tab = ui.tab("Chat")

    with ui.tab_panels(tabs, value=timeline_tab).classes("w-full"):
        with ui.tab_panel(timeline_tab):
            _render_timeline_tab(context)
        with ui.tab_panel(report_tab):
            _render_report_tab(context)
        with ui.tab_panel(chat_tab):
            _render_chat_tab(context)


@ui.page("/job/{job_id}")
@require_auth
def job_detail_page(job_id: str) -> None:
    """Job detail page with progressive disclosure UI."""
    context = _build_job_detail_context(job_id)
    if context is None:
        _render_job_not_found()
        return

    ui.page_title(f"{_job_page_title(context.job_info)} - OpenScientist")
    render_navigator()
    _render_job_status_notices(context)
    _render_job_tabs(context)
