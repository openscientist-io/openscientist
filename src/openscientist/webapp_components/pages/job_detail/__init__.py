"""Job detail page shell: context, notices and tab wiring."""

import logging
from typing import Any
from uuid import UUID

from nicegui import ui
from sqlalchemy import select

from openscientist.auth import get_current_user_id, require_auth
from openscientist.job.repository import get_job, get_share_permission
from openscientist.job.types import JobInfo, JobStatus
from openscientist.webapp_components.components.banners import render_error_card
from openscientist.webapp_components.components.dialogs import (
    render_delete_dialog,
    render_share_dialog,
)
from openscientist.webapp_components.components.navigation import render_navigator
from openscientist.webapp_components.components.notifications import (
    render_notifications_dialog,
)
from openscientist.webapp_components.error_handler import get_user_friendly_error
from openscientist.webapp_components.pages.job_detail.chat import _render_chat_tab
from openscientist.webapp_components.pages.job_detail.common import (
    _chat_tab_tooltip,
    _derive_progress_from_ks,
    _JobDetailContext,
    _load_attachments,
    _load_knowledge_state,
    _page_snapshot,
    _PageSnapshot,
    _report_tab_tooltip,
)
from openscientist.webapp_components.pages.job_detail.prompt import add_prompt_head_html
from openscientist.webapp_components.pages.job_detail.report import _render_report_tab
from openscientist.webapp_components.pages.job_detail.timeline import (
    _prompt_refining,
    _render_timeline_tab,
    _stats_model,
    _StatsModel,
)

logger = logging.getLogger(__name__)


def _render_job_not_found() -> None:
    ui.label("Job not found").classes("text-h5")
    ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"))


async def _load_db_job_for_user(job_id: str, user_id: str) -> Any:
    try:
        return await get_job(job_id, user_id=UUID(user_id))
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


async def _resolve_job_permissions(
    job_id: str, user_id: str, db_job: Any
) -> tuple[bool, bool, bool]:
    """Whether the caller is the owner, may edit through a share, and reads the
    job as its owner or through a share rather than only because it is public.
    Those direct readers are the ones the cost_records policies admit and the
    ones the job's uploads are served to."""
    is_owner = db_job.owner_id == UUID(user_id)
    if is_owner:
        return True, True, True
    share_permission = await get_share_permission(job_id, UUID(user_id))
    return False, share_permission == "edit", share_permission is not None


async def _owner_name(job_id: str, is_owner: bool) -> str | None:
    """The owner's display name for a viewer who is not the owner, name only.

    Read through the admin session, because the web login's RLS hides other
    users' rows. None for the owner, who needs no label.
    """
    if is_owner:
        return None
    from openscientist.database.models import Job as JobModel
    from openscientist.database.models import User
    from openscientist.database.session import get_admin_session

    async with get_admin_session() as session:
        result = await session.execute(
            select(User.name)
            .join(JobModel, JobModel.owner_id == User.id)
            .where(JobModel.id == UUID(job_id))
        )
        return result.scalar_one_or_none()


def _job_page_title(job_info: Any) -> str:
    page_title: str = job_info.short_title or job_info.research_question[:50]
    if len(job_info.research_question) > 50 and not job_info.short_title:
        page_title += "..."
    return page_title


def _initial_job_state(
    job_info: JobInfo, stats_model: _StatsModel, snapshot: _PageSnapshot | None
) -> dict[str, Any]:
    """The per-client state of the job page, one entry per polled value the
    page compares before redrawing. The snapshot is the tick's baseline, the
    probe the page ran while it was built."""
    return {
        "status": job_info.status,
        "stats_model": stats_model,
        "snapshot": snapshot,
    }


def _create_page_dialogs(
    job_id: str, job_manager: Any, user_id: str, is_owner: bool
) -> tuple[Any, Any, Any]:
    share_dialog = render_share_dialog(job_id) if is_owner else None
    delete_dialog = render_delete_dialog(
        job_id,
        job_manager,
        on_deleted=lambda: ui.navigate.to("/jobs"),
    )
    notifications_dialog = render_notifications_dialog(job_id, user_id)
    return share_dialog, delete_dialog, notifications_dialog


async def _build_job_detail_context(job_id: str) -> _JobDetailContext | None:
    from openscientist import web_app

    job_manager = web_app.get_job_manager()
    user_id = get_current_user_id()
    assert user_id is not None
    db_job = await _load_db_job_for_user(job_id, user_id)
    if db_job is None:
        return None

    is_owner, can_edit, direct_access = await _resolve_job_permissions(job_id, user_id, db_job)
    owner_name = await _owner_name(job_id, is_owner)

    job_dir = job_manager.jobs_dir / job_id
    ks_data, action_counts, ks_load_error = await _load_knowledge_state(job_id, user_id)

    # Derive progress from already-loaded KS data instead of loading it again via get_job()
    iterations_completed, findings_count = _derive_progress_from_ks(
        ks_data, db_job.status, db_job.current_iteration
    )
    job_info = JobInfo.from_db_model(db_job, iterations_completed, findings_count)
    probe = await _page_snapshot(job_id, user_id, job_dir)
    snapshot = probe[1] if probe is not None else None
    spend = snapshot.job.spend if snapshot is not None else 0.0
    attachments = await _load_attachments(job_id, user_id) if direct_access else []
    share_dialog, delete_dialog, notifications_dialog = _create_page_dialogs(
        job_id,
        job_manager,
        user_id,
        is_owner,
    )

    return _JobDetailContext(
        job_id=job_id,
        user_id=user_id,
        job_manager=job_manager,
        job_info=job_info,
        is_owner=is_owner,
        can_edit=can_edit,
        owner_name=owner_name,
        job_dir=job_dir,
        ks_data=ks_data,
        action_counts=action_counts,
        ks_load_error=ks_load_error,
        spend=spend,
        spend_visible=direct_access,
        state=_initial_job_state(
            job_info,
            _stats_model(
                job_info,
                ks_data,
                spend if direct_access else None,
                _prompt_refining(job_info, action_counts),
            ),
            snapshot,
        ),
        share_dialog=share_dialog,
        delete_dialog=delete_dialog,
        notifications_dialog=notifications_dialog,
        attachments=attachments,
    )


def _render_cancelled_notice(job_info: JobInfo) -> None:
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


def _tab_pane(tabs: ui.tabs, tab: ui.tab) -> ui.element:
    """The content of ``tab``, hidden while another tab is selected.

    Quasar's tab panels detach a hidden panel from the document, and a
    detached iframe reloads when it returns, so the report would be fetched
    and rendered again on every visit. A hidden pane stays in the document,
    and NiceGUI's tab panel class gives it the same padding and section gap.
    """
    name = tab.props["name"]
    return (
        ui.element("div")
        .classes("nicegui-tab-panel w-full")
        .bind_visibility_from(tabs, "value", backward=lambda value: value in (tab, name))
    )


async def _render_job_tabs(context: _JobDetailContext) -> None:
    # Computed once at render: the page reloads on every status that unlocks a
    # tab, so a tab never has to be re-enabled by the pollers.
    report_tooltip = _report_tab_tooltip(context.job_info.status)
    chat_tooltip = _chat_tab_tooltip(context.job_info.status)
    with ui.tabs().classes("w-full") as tabs:
        timeline_tab = ui.tab("Research Log")
        report_tab = ui.tab("Report")
        if report_tooltip is not None:
            report_tab.props("disable").classes("os-tab-disabled")
            with report_tab:
                ui.tooltip(report_tooltip)
        chat_tab = ui.tab("Chat")
        if chat_tooltip is not None:
            chat_tab.props("disable").classes("os-tab-disabled")
            with chat_tab:
                ui.tooltip(chat_tooltip)

    tabs.set_value(timeline_tab)

    with _tab_pane(tabs, timeline_tab):
        await _render_timeline_tab(context)
    with _tab_pane(tabs, report_tab):
        _render_report_tab(context, tabs, report_tab)
    with _tab_pane(tabs, chat_tab):
        await _render_chat_tab(context)


@ui.page("/job/{job_id}")
@require_auth
async def job_detail_page(job_id: str) -> None:
    """Job detail page with progressive disclosure UI."""
    context = await _build_job_detail_context(job_id)
    if context is None:
        _render_job_not_found()
        return

    ui.page_title(f"{_job_page_title(context.job_info)} - OpenScientist")
    add_prompt_head_html()
    render_navigator()
    _render_job_status_notices(context)
    await _render_job_tabs(context)
