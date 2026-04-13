"""Job detail page with progressive disclosure UI.

Uses NiceGUI's websocket-based updates for real-time UI changes.
The page uses @ui.refreshable decorators to enable in-place updates
without full page reloads.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from nicegui import ui

from openscientist.artifact_packager import create_artifacts_zip
from openscientist.async_tasks import run_sync
from openscientist.auth import get_current_user_id, require_auth
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session_ctx
from openscientist.job.types import JobInfo, JobStatus
from openscientist.job_chat import get_chat_history, send_chat_message
from openscientist.job_manager import _db_get_job, _db_get_share_permission
from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import update_job_status
from openscientist.pdf_generator import markdown_to_pdf
from openscientist.webapp_components.error_handler import get_user_friendly_error
from openscientist.webapp_components.ui_components import (
    STATUS_COLORS,
    _inject_pubmed_badge_styles,
    render_delete_dialog,
    render_error_card,
    render_job_action_buttons,
    render_justified_text,
    render_navigator,
    render_notifications_dialog,
    render_pmid_badge,
    render_share_dialog,
    render_stat_badges,
    render_text_with_pmid_links,
    render_thinking_status,
    transform_pmid_references,
)
from openscientist.webapp_components.utils import (
    ClientGuard,
    guard_client,
    is_client_connected,
    safe_run_javascript,
    setup_timer_cleanup,
)

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


def _show_no_timeline_activity() -> None:
    ui.label("No investigation activity yet").classes("text-gray-500")


def _timeline_iteration_summaries(timeline_ks: dict[str, Any]) -> dict[int, dict[str, str]]:
    summaries: dict[int, dict[str, str]] = {}
    for entry in timeline_ks.get("iteration_summaries", []):
        if not isinstance(entry, dict):
            continue
        iteration = entry.get("iteration")
        if not isinstance(iteration, int):
            continue
        summaries[iteration] = {
            "summary": entry.get("summary", ""),
            "strapline": entry.get("strapline", ""),
        }
    return summaries


def _timeline_entries_by_iteration(
    timeline_ks: dict[str, Any],
) -> defaultdict[int, list[Any]]:
    by_iteration: defaultdict[int, list[Any]] = defaultdict(list)
    for entry in timeline_ks.get("analysis_log", []):
        iteration = entry.get("iteration")
        if isinstance(iteration, int):
            by_iteration[iteration].append(entry)
    return by_iteration


def _normalize_iteration_summary(iter_summary: Any) -> tuple[str, str]:
    if isinstance(iter_summary, str):
        return "", iter_summary
    if not isinstance(iter_summary, dict):
        return "", ""
    return iter_summary.get("strapline", ""), iter_summary.get("summary", "")


def _iteration_activity_counts(entries: list[Any]) -> tuple[int, int, int]:
    code_count = sum(1 for entry in entries if entry.get("action") == "execute_code")
    search_count = sum(1 for entry in entries if entry.get("action") == "search_pubmed")
    finding_count = sum(1 for entry in entries if entry.get("action") == "update_knowledge_state")
    return code_count, search_count, finding_count


def _timeline_border_class(code_count: int, search_count: int, finding_count: int) -> str:
    if finding_count > 0:
        return "border-l-4 border-green-500"
    if code_count > 0 or search_count > 0:
        return "border-l-4 border-blue-300"
    return "border-l-4 border-gray-300"


def _timeline_header_text(strapline: str, summary_text: str, is_in_progress: bool) -> str:
    if strapline:
        base_text = strapline
    elif summary_text:
        base_text = summary_text[:80] + "..." if len(summary_text) > 80 else summary_text
    elif is_in_progress:
        return "Investigation in progress..."
    else:
        return "Completed"
    return f"{base_text} [in progress]" if is_in_progress else base_text


def _action_card_class(tool_name: str) -> str:
    if "execute_code" in tool_name:
        return "w-full mb-2 border-l-4 border-blue-300"
    if "search_pubmed" in tool_name:
        return "w-full mb-2 border-l-4 border-purple-300"
    if "update_knowledge_state" in tool_name:
        return "w-full mb-2 border-l-4 border-green-300"
    return "w-full mb-2 border-l-4 border-gray-300"


@dataclass(frozen=True)
class _AnalysisLogMetaLine:
    text: str
    italic: bool = False


def _analysis_log_meta_lines(entry: dict[str, Any]) -> list[_AnalysisLogMetaLine]:
    action_type = entry.get("action", "")
    lines: list[_AnalysisLogMetaLine] = []

    if "search_pubmed" in action_type:
        if query := entry.get("query"):
            lines.append(_AnalysisLogMetaLine(f'Query: "{query}"'))
        if (count := entry.get("results_count")) is not None:
            lines.append(_AnalysisLogMetaLine(f"Papers found: {count}"))
    elif "add_hypothesis" in action_type or "update_hypothesis" in action_type:
        if statement := entry.get("statement"):
            lines.append(_AnalysisLogMetaLine(statement, italic=True))
        if status := entry.get("status"):
            lines.append(_AnalysisLogMetaLine(f"Status: {status}"))
        if summary := entry.get("result_summary"):
            lines.append(_AnalysisLogMetaLine(summary))
    elif "update_knowledge_state" in action_type:
        if title := entry.get("title"):
            lines.append(_AnalysisLogMetaLine(f"Finding: {title}"))
    elif "run_phenix_tool" in action_type:
        if tool_name := entry.get("tool_name"):
            lines.append(_AnalysisLogMetaLine(f"Tool: {tool_name}"))

    if "execute_code" in action_type and (exec_time := entry.get("execution_time")) is not None:
        lines.append(_AnalysisLogMetaLine(f"Duration: {exec_time}s"))

    return lines


def _render_analysis_log_details(entry: dict[str, Any], success: bool) -> None:
    meta = "text-xs text-gray-600 mt-1"
    action_type = entry.get("action", "")

    for line in _analysis_log_meta_lines(entry):
        label = ui.label(line.text).classes(meta)
        if line.italic:
            label.style("font-style: italic")

    code = entry.get("code")
    if code and "execute_code" in action_type:
        with ui.expansion("Code", icon="code").classes("w-full mt-1"):
            ui.code(code, language="python").classes("text-xs")

    output = entry.get("output")
    if not output:
        return
    output_str = str(output)
    if len(output_str) > 200:
        with ui.expansion("Result", icon="output").classes("w-full mt-1"):
            ui.code(
                output_str[:2000] + ("..." if len(output_str) > 2000 else ""),
                language="text",
            ).classes("text-xs")
    elif not success:
        ui.label(output_str).classes("text-xs text-red-600 mt-1")
    else:
        ui.label(output_str).classes(meta)


def _render_analysis_log_actions(entries: list[Any]) -> None:
    if not entries:
        return
    with ui.expansion(
        f"Actions ({len(entries)})",
        icon="build",
    ).classes("w-full mt-2"):
        for entry in entries:
            success = entry.get("success", True)
            action_type = entry.get("action", "")
            description = entry.get("description", action_type or "Unknown")
            status_icon = "\u2705" if success else "\u274c"

            with ui.card().classes(_action_card_class(action_type)):
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"{status_icon} {description}").classes("font-medium text-sm")
                    ui.badge(action_type, color="gray").props("outline").classes("text-xs")
                _render_analysis_log_details(entry, success)


def _collect_iteration_plots(
    iter_provenance_dir: Path, iter_num: int
) -> list[tuple[Path, dict[str, Any]]]:
    plots: list[tuple[Path, dict[str, Any]]] = []
    if not iter_provenance_dir.exists():
        return plots
    for plot_file in sorted(iter_provenance_dir.glob("*.png")):
        metadata_file = plot_file.with_suffix(".json")
        if not metadata_file.exists():
            continue
        try:
            with open(metadata_file, encoding="utf-8") as metadata_handle:
                metadata = json.load(metadata_handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if metadata.get("iteration") == iter_num:
            plots.append((plot_file, metadata))
    return plots


def _render_iteration_plots(iter_provenance_dir: Path, iter_num: int) -> None:
    iteration_plots = _collect_iteration_plots(iter_provenance_dir, iter_num)
    if not iteration_plots:
        return

    with (
        ui.expansion(
            f"Visualizations ({len(iteration_plots)})",
            icon="insert_chart",
        ).classes("w-full mt-2"),
        ui.grid(columns=2).classes("w-full gap-2"),
    ):
        for plot_file, metadata in iteration_plots:
            plot_title = plot_file.stem.replace("_", " ").title()
            description = metadata.get("description", "")
            with ui.card().classes("p-2"):
                ui.label(plot_title).classes("text-sm font-bold")
                if description:
                    ui.label(description).classes("text-xs text-blue-700 italic")
                plot_url = f"/{plot_file}"
                ui.image(plot_url).classes("w-full")
                ui.button(
                    "Download",
                    on_click=lambda p=plot_file: ui.download(p.read_bytes(), filename=p.name),
                    icon="download",
                ).props("size=sm flat dense").classes("mt-2")
                plot_code = metadata.get("code")
                if plot_code:
                    with ui.expansion("View code", icon="code").classes("w-full mt-1"):
                        ui.code(plot_code, language="python").classes("text-xs")


def _matching_papers(iter_ks_data: dict[str, Any], query: str, iter_num: int) -> list[Any]:
    return [
        literature
        for literature in iter_ks_data.get("literature", [])
        if literature.get("search_query") == query
        and literature.get("retrieved_at_iteration") == iter_num
    ]


def _render_literature_paper(paper: dict[str, Any]) -> None:
    with ui.card().classes("w-full mb-1 p-2"):
        ui.label(paper.get("title", "Untitled")).classes("text-sm font-bold")
        pmid = paper.get("pmid", "")
        if pmid:
            render_pmid_badge(pmid)
        abstract = paper.get("abstract", "")
        if abstract:
            preview = abstract[:200] + "..." if len(abstract) > 200 else abstract
            ui.label(preview).classes("text-xs text-gray-600 mt-1")


def _render_iteration_literature(
    iter_entries: list[Any], iter_ks_data: dict[str, Any], iter_num: int
) -> None:
    literature_entries = [entry for entry in iter_entries if entry.get("action") == "search_pubmed"]
    if not literature_entries:
        return

    all_iter_papers = [
        lit
        for lit in iter_ks_data.get("literature", [])
        if lit.get("retrieved_at_iteration") == iter_num
    ]

    total_papers = sum(entry.get("results_count", 0) for entry in literature_entries)
    rendered_queries: set[str] = set()
    with ui.expansion(
        f"Literature searched ({total_papers or len(all_iter_papers)} papers)",
        icon="article",
    ).classes("w-full mt-2"):
        for entry in literature_entries:
            query = entry.get("query", "")
            if not query:
                continue
            rendered_queries.add(query)
            matching = _matching_papers(iter_ks_data, query, iter_num)
            if matching:
                with ui.expansion(f'"{query}" ({len(matching)} papers)').classes("w-full"):
                    for paper in matching:
                        _render_literature_paper(paper)
            else:
                ui.label(f'"{query}" (0 results)').classes("text-sm text-gray-400 italic")

        # Fallback: show papers whose search_query wasn't covered by analysis log entries
        # (e.g. migrated jobs where query/results_count were not preserved)
        remaining: dict[str, list[Any]] = {}
        for paper in all_iter_papers:
            sq = paper.get("search_query") or ""
            if sq not in rendered_queries:
                remaining.setdefault(sq, []).append(paper)
        for sq, papers in remaining.items():
            label = f'"{sq}" ({len(papers)} papers)' if sq else f"{len(papers)} papers"
            with ui.expansion(label).classes("w-full"):
                for paper in papers:
                    _render_literature_paper(paper)


_HYPOTHESIS_STATUS_CLASSES: dict[str, str] = {
    "supported": "bg-green-50 border-l-4 border-green-500",
    "rejected": "bg-red-50 border-l-4 border-red-400",
    "testing": "bg-blue-50 border-l-4 border-blue-400",
    "pending": "bg-gray-50",
}

_HYPOTHESIS_STATUS_LABEL_CLASSES: dict[str, str] = {
    "supported": "text-green-700",
    "rejected": "text-red-700",
    "testing": "text-blue-700",
    "pending": "text-gray-500",
}


def _render_iteration_hypotheses(iter_ks_data: dict[str, Any], iter_num: int) -> None:
    iter_hypotheses = [
        h
        for h in iter_ks_data.get("hypotheses", [])
        if h.get("iteration_proposed") == iter_num or h.get("iteration_tested") == iter_num
    ]
    if not iter_hypotheses:
        return

    with ui.expansion(f"Hypotheses ({len(iter_hypotheses)})", icon="science").classes(
        "w-full mt-2"
    ):
        for hyp in iter_hypotheses:
            status = hyp.get("status", "pending")
            card_class = _HYPOTHESIS_STATUS_CLASSES.get(status, "bg-gray-50")
            label_class = _HYPOTHESIS_STATUS_LABEL_CLASSES.get(status, "text-gray-500")
            with ui.card().classes(f"w-full mb-2 {card_class}"):
                ui.label(hyp.get("statement", "")).classes("font-bold text-gray-800 text-sm")
                ui.label(f"Status: {status}").classes(f"text-xs {label_class} mt-1")
                result = hyp.get("result") or {}
                if result.get("summary"):
                    ui.label(result["summary"]).classes("text-sm text-gray-700 mt-1")
                if result.get("conclusion"):
                    ui.label(result["conclusion"]).classes("text-sm text-gray-600 italic mt-1")


def _render_iteration_findings(iter_ks_data: dict[str, Any], iter_num: int) -> None:
    iteration_findings = [
        finding
        for finding in iter_ks_data.get("findings", [])
        if finding.get("iteration_discovered") == iter_num
    ]
    if not iteration_findings:
        return

    with ui.expansion(f"Findings ({len(iteration_findings)})", icon="lightbulb").classes(
        "w-full mt-2"
    ):
        for finding in iteration_findings:
            with ui.card().classes("w-full mb-2 bg-green-50"):
                ui.label(finding["title"]).classes("font-bold text-green-800")
                render_text_with_pmid_links(
                    finding["evidence"],
                    text_classes="text-sm text-gray-700",
                )
                interpretation = finding.get("biological_interpretation") or finding.get(
                    "interpretation", ""
                )
                if interpretation:
                    render_justified_text(
                        interpretation,
                        text_classes="text-sm text-gray-600 italic mt-1",
                    )


def _load_iteration_content(
    container: ui.column,
    loaded_flag: dict[str, Any],
    iter_num: int,
    iter_summary_text: str,
    iter_entries: list[Any],
    iter_ks_data: dict[str, Any],
    iter_provenance_dir: Path,
) -> None:
    if not is_client_connected() or loaded_flag["value"]:
        return
    loaded_flag["value"] = True
    container.clear()

    with container:
        if iter_summary_text:
            with ui.expansion("Summary", icon="summarize", value=True).classes("w-full mt-2"):
                render_text_with_pmid_links(
                    iter_summary_text,
                    text_classes="text-sm text-gray-700",
                )

        _render_iteration_hypotheses(iter_ks_data, iter_num)
        _render_iteration_findings(iter_ks_data, iter_num)
        _render_analysis_log_actions(iter_entries)
        _render_iteration_plots(iter_provenance_dir, iter_num)
        _render_iteration_literature(iter_entries, iter_ks_data, iter_num)


def _render_iteration_header(
    iteration: int,
    header_text: str,
    code_count: int,
    search_count: int,
    finding_count: int,
    hypothesis_count: int = 0,
) -> None:
    with ui.row().classes("items-center gap-2 flex-wrap"):
        ui.label(f"Iteration {iteration}: {header_text}").classes("font-medium")
        if hypothesis_count:
            ui.badge(f"{hypothesis_count} hypotheses", color="orange")
        if code_count:
            ui.badge(f"{code_count} analyses", color="blue").props("outline")
        if search_count:
            ui.badge(f"{search_count} searches", color="purple").props("outline")
        if finding_count:
            ui.badge(f"{finding_count} findings", color="green")


def _render_iteration_card(
    iteration: int,
    entries: list[Any],
    iter_summary: Any,
    timeline_ks: dict[str, Any],
    timeline_max_iter: int,
    latest_status: JobStatus,
    iter_provenance_dir: Path,
) -> None:
    is_in_progress = iteration == timeline_max_iter and latest_status == JobStatus.RUNNING
    strapline, summary_text = _normalize_iteration_summary(iter_summary)
    code_count, search_count, finding_count = _iteration_activity_counts(entries)
    hypothesis_count = sum(
        1
        for h in timeline_ks.get("hypotheses", [])
        if h.get("iteration_proposed") == iteration or h.get("iteration_tested") == iteration
    )
    border_class = _timeline_border_class(code_count, search_count, finding_count)
    header_text = _timeline_header_text(strapline, summary_text, is_in_progress)

    with ui.expansion(icon="science").classes(f"w-full mb-2 {border_class}") as expansion:
        with expansion.add_slot("header"):
            _render_iteration_header(
                iteration,
                header_text,
                code_count,
                search_count,
                finding_count,
                hypothesis_count,
            )

        content_container = ui.column().classes("w-full")
        content_loaded = {"value": False}
        with content_container:
            ui.label("Click to load details...").classes("text-sm text-gray-400 italic")
        expansion.on_value_change(
            lambda e, cc=content_container, lf=content_loaded, iter_num=iteration, summary=summary_text, iter_data=entries, ks_data=timeline_ks, provenance_dir=iter_provenance_dir: (
                _load_iteration_content(
                    cc,
                    lf,
                    iter_num,
                    summary,
                    iter_data,
                    ks_data,
                    provenance_dir,
                )
                if e.value
                else None
            )
        )


def _render_timeline_content(timeline_ks: dict[str, Any], latest_job: Any, job_dir: Path) -> None:
    timeline_iteration_summaries = _timeline_iteration_summaries(timeline_ks)
    timeline_by_iteration = _timeline_entries_by_iteration(timeline_ks)
    timeline_max_iter = timeline_ks.get("iteration", 1)

    if not timeline_by_iteration and not timeline_iteration_summaries:
        _show_no_timeline_activity()
        return

    display_max = (
        timeline_max_iter - 1
        if latest_job.status == JobStatus.AWAITING_FEEDBACK
        else timeline_max_iter
    )
    iter_provenance_dir = job_dir / "provenance"
    with ui.scroll_area().classes("w-full h-[600px]"):
        for iteration in range(1, display_max + 1):
            _render_iteration_card(
                iteration=iteration,
                entries=timeline_by_iteration.get(iteration, []),
                iter_summary=timeline_iteration_summaries.get(iteration, {}),
                timeline_ks=timeline_ks,
                timeline_max_iter=timeline_max_iter,
                latest_status=latest_job.status,
                iter_provenance_dir=iter_provenance_dir,
            )


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


def _format_model_display(llm_model: str | None, llm_provider: str | None) -> str | None:
    """Map raw model IDs to human-readable names."""
    if not llm_model:
        return llm_provider.title() if llm_provider else None

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
    model_display = _format_model_display(
        getattr(latest_job, "llm_model", None),
        getattr(latest_job, "llm_provider", None),
    )
    if model_display:
        badges.append(("Model", model_display, "cyan"))
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


def _download_artifacts_zip(job_dir: Path, job_id: str) -> None:
    try:
        zip_buffer = create_artifacts_zip(job_dir, job_id)
        ui.download(zip_buffer.getvalue(), filename=f"{job_id}_artifacts.zip")
    except Exception as exc:
        logger.error("Failed to create artifacts ZIP: %s", exc, exc_info=True)
        ui.notify("Failed to create ZIP. Please try again.", type="negative")


def _download_pdf_report(report_path: Path, pdf_path: Path, job_id: str) -> None:
    # Serve existing PDF if available (avoids overwriting WeasyPrint PDF with fpdf2)
    if pdf_path.exists():
        ui.download(pdf_path.read_bytes(), filename=f"{job_id}_report.pdf")
        return
    # Fallback: generate via fpdf2
    try:
        from openscientist.report.processor import strip_figure_tags

        raw_md = report_path.read_text(encoding="utf-8")
        stripped = strip_figure_tags(raw_md)
        stripped_path = report_path.parent / "_final_report_stripped.md"
        stripped_path.write_text(stripped, encoding="utf-8")
        try:
            markdown_to_pdf(stripped_path, pdf_path)
        finally:
            stripped_path.unlink(missing_ok=True)
        ui.download(pdf_path.read_bytes(), filename=f"{job_id}_report.pdf")
    except Exception as exc:
        logger.error("PDF generation failed: %s", exc, exc_info=True)
        ui.notify("Failed to generate PDF. Please try again.", type="negative")


def _render_report_actions(context: _JobDetailContext, report_path: Path, pdf_path: Path) -> None:
    with ui.row().classes("w-full justify-end mb-4 gap-2"):
        if pdf_path.exists() or report_path.exists():
            ui.button(
                "Download PDF",
                on_click=lambda: _download_pdf_report(report_path, pdf_path, context.job_id),
                icon="picture_as_pdf",
            ).props("color=primary")
        else:
            ui.button("PDF Unavailable", icon="picture_as_pdf").props("color=grey outline disabled")

        ui.button(
            "Download All Artifacts",
            on_click=lambda: _download_artifacts_zip(context.job_dir, context.job_id),
            icon="folder_zip",
        ).props("color=accent outline")


def _render_report_html_iframe(job_dir: Path) -> None:
    """Render HTML report in an iframe to avoid CSS leakage."""
    from nicegui import app

    html_path = job_dir / "final_report.html"
    if not html_path.exists():
        return

    # Re-render with base64 images for browser display
    try:
        from openscientist.report.renderer import render_report_html

        md_path = job_dir / "final_report.md"
        html_content = render_report_html(md_path, job_dir, embed_images=True)
    except Exception:
        logger.warning("Failed to re-render HTML with base64 images, using on-disk version")
        html_content = html_path.read_text(encoding="utf-8")

    # Serve as a static route and embed in iframe
    route_path = f"/report-html/{job_dir.name}"

    @app.get(route_path)
    async def _serve_report_html():  # type: ignore[no-untyped-def]
        from starlette.responses import HTMLResponse

        return HTMLResponse(html_content)

    ui.element("iframe").props(f'src="{route_path}" frameborder="0"').style(
        "width: 100%; height: 80vh; border: 1px solid #ddd; border-radius: 8px;"
    )


def _render_report_markdown(report_path: Path) -> None:
    with open(report_path, encoding="utf-8") as report_file:
        report_content = report_file.read()
    _inject_pubmed_badge_styles()
    ui.markdown(transform_pmid_references(report_content)).classes("w-full")


def _render_missing_report_state(context: _JobDetailContext) -> None:
    if context.job_info.status == JobStatus.GENERATING_REPORT:
        ui.label("Report is being generated...").classes("text-gray-500 italic")
        return
    if context.job_info.status in [JobStatus.COMPLETED, JobStatus.FAILED]:
        ui.label("Report generation failed").classes("text-red-500")
        return
    ui.label("Report will be available when job completes").classes("text-gray-500 italic")


def _render_report_tab(context: _JobDetailContext) -> None:
    report_path = context.job_dir / "final_report.md"
    html_path = context.job_dir / "final_report.html"
    pdf_path = context.job_dir / "final_report.pdf"

    if context.job_info.status == JobStatus.GENERATING_REPORT:
        render_thinking_status("Generating report...")

    # Only show the report when the job has finished. The agent may write
    # final_report.md mid-run, but it is not ready for display until the
    # orchestrator marks the job completed (or failed).
    if context.job_info.status not in (JobStatus.COMPLETED, JobStatus.FAILED):
        _render_missing_report_state(context)
        return

    if report_path.exists():
        _render_report_actions(context, report_path, pdf_path)
        # Prefer HTML report (with embedded figures) over raw markdown
        if html_path.exists():
            _render_report_html_iframe(context.job_dir)
        else:
            _render_report_markdown(report_path)
        return

    _render_missing_report_state(context)


_CHAT_STYLES = """
<style>
    .chat-bubble-user {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 18px 18px 4px 18px;
        padding: 12px 16px;
        color: white;
        max-width: 85%;
        margin-left: auto;
        word-wrap: break-word;
        box-shadow: 0 2px 8px rgba(102, 126, 234, 0.3);
    }
    .chat-bubble-assistant {
        background: linear-gradient(135deg, #e0f2fe 0%, #cffafe 100%);
        border-radius: 18px 18px 18px 4px;
        padding: 12px 16px;
        color: #0c4a6e;
        max-width: 85%;
        word-wrap: break-word;
        box-shadow: 0 2px 8px rgba(14, 116, 144, 0.15);
        border: 1px solid #a5f3fc;
    }
    .chat-bubble-assistant .markdown-body {
        background: transparent !important;
    }
    .chat-container {
        background: linear-gradient(180deg, #fafbfc 0%, #f0f2f5 100%);
        border-radius: 12px;
        border: 1px solid #e1e4e8;
    }
    .chat-input-container {
        background: white;
        border-radius: 24px;
        border: 2px solid #e1e4e8;
        transition: border-color 0.2s, box-shadow 0.2s;
        min-height: 48px;
    }
    .chat-input-container:focus-within {
        border-color: #667eea;
        box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
    }
    .chat-input-row {
        align-items: center !important;
    }
    .chat-send-btn {
        width: 48px !important;
        height: 48px !important;
        min-width: 48px !important;
        min-height: 48px !important;
    }
</style>
"""


_CHAT_SCROLL_OBSERVER_SCRIPT = """
if (!window._chatScrollObserver) {
    window._chatScrollObserver = new MutationObserver(() => {
        const el = document.querySelector('.chat-messages-scroll');
        if (el && el.getBoundingClientRect().width > 0) {
            window._chatScrollObserver.disconnect();
            window._chatScrollObserver = null;
            const scroll = () => {
                const c = el.querySelector('.q-scrollarea__container');
                if (c) c.scrollTop = c.scrollHeight;
            };
            [50, 150, 300].forEach(ms => setTimeout(scroll, ms));
        }
    });
    window._chatScrollObserver.observe(document.body, {
        childList: true, subtree: true, attributes: true
    });
}
"""


_CHAT_HEADER_SVG = """
<svg viewBox="0 0 100 100" width="28" height="28" xmlns="http://www.w3.org/2000/svg">
    <path d="M22 18 Q50 18 50 40 Q50 60 78 60 Q78 82 50 82 Q22 82 22 60"
          fill="none" stroke="#0891b2" stroke-width="10" stroke-linecap="round"/>
    <circle cx="22" cy="18" r="10" fill="#06b6d4"/>
    <circle cx="78" cy="60" r="10" fill="#06b6d4"/>
    <circle cx="22" cy="60" r="10" fill="#0e7490"/>
</svg>
"""


_CHAT_AVATAR_HTML = """
<div style="width: 32px; height: 32px; background: #e0f2fe; border-radius: 50%; padding: 4px; flex-shrink: 0;">
    <svg viewBox="0 0 100 100" width="24" height="24" xmlns="http://www.w3.org/2000/svg">
        <path d="M22 18 Q50 18 50 40 Q50 60 78 60 Q78 82 50 82 Q22 82 22 60"
              fill="none" stroke="#0891b2" stroke-width="12" stroke-linecap="round"/>
        <circle cx="22" cy="18" r="10" fill="#06b6d4"/>
        <circle cx="78" cy="60" r="10" fill="#06b6d4"/>
        <circle cx="22" cy="60" r="10" fill="#0e7490"/>
    </svg>
</div>
"""


def _chat_sound_script(sound_type: str) -> str:
    return f"""
    (function() {{
        try {{
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const type = '{sound_type}';

            if (type === 'sound-send') {{
                const osc1 = ctx.createOscillator();
                const gain1 = ctx.createGain();
                osc1.connect(gain1);
                gain1.connect(ctx.destination);
                osc1.frequency.setValueAtTime(600, ctx.currentTime);
                osc1.frequency.exponentialRampToValueAtTime(900, ctx.currentTime + 0.08);
                osc1.type = 'sine';
                gain1.gain.setValueAtTime(0, ctx.currentTime);
                gain1.gain.linearRampToValueAtTime(0.15, ctx.currentTime + 0.02);
                gain1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.12);
                osc1.start(ctx.currentTime);
                osc1.stop(ctx.currentTime + 0.15);
            }} else if (type === 'sound-receive') {{
                const osc1 = ctx.createOscillator();
                const osc2 = ctx.createOscillator();
                const gain1 = ctx.createGain();
                const gain2 = ctx.createGain();
                osc1.connect(gain1);
                osc2.connect(gain2);
                gain1.connect(ctx.destination);
                gain2.connect(ctx.destination);
                osc1.type = 'sine';
                osc2.type = 'sine';
                osc1.frequency.value = 880;
                osc2.frequency.value = 1100;
                gain1.gain.setValueAtTime(0, ctx.currentTime);
                gain1.gain.linearRampToValueAtTime(0.12, ctx.currentTime + 0.02);
                gain1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
                gain2.gain.setValueAtTime(0, ctx.currentTime + 0.08);
                gain2.gain.linearRampToValueAtTime(0.1, ctx.currentTime + 0.1);
                gain2.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
                osc1.start(ctx.currentTime);
                osc2.start(ctx.currentTime + 0.08);
                osc1.stop(ctx.currentTime + 0.3);
                osc2.stop(ctx.currentTime + 0.35);
            }} else if (type === 'sound-error') {{
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.type = 'sine';
                osc.frequency.setValueAtTime(440, ctx.currentTime);
                osc.frequency.exponentialRampToValueAtTime(280, ctx.currentTime + 0.2);
                gain.gain.setValueAtTime(0, ctx.currentTime);
                gain.gain.linearRampToValueAtTime(0.12, ctx.currentTime + 0.02);
                gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
                osc.start(ctx.currentTime);
                osc.stop(ctx.currentTime + 0.3);
            }}
        }} catch(e) {{}}
    }})();
    """


class _ChatTabController:
    def __init__(self, context: _JobDetailContext) -> None:
        self.context = context
        self.job_uuid = UUID(context.job_id)
        self.chat_scroll: Any = None
        self.status_container: Any = None
        self.chat_input: Any = None
        self.send_btn: Any = None

    def render(self) -> None:
        if self.context.job_info.status != JobStatus.COMPLETED:
            ui.label("Chat will be available when the job completes.").classes(
                "text-gray-500 italic"
            )
            return

        ui.add_head_html(_CHAT_STYLES)
        self._render_shell()
        self.context.active_timers.append(ui.timer(0.1, self._render_messages, once=True))

        if self.context.can_edit:
            self._render_input_area()
            return
        ui.label("You have view-only access to this job.").classes(
            "text-sm text-gray-500 italic mt-4 text-center"
        )

    def _render_shell(self) -> None:
        with (
            ui.column()
            .classes("w-full max-w-4xl mx-auto chat-container p-4 flex flex-col flex-nowrap")
            .style("height: calc(100vh - 280px); min-height: 500px;")
        ):
            with ui.row().classes("w-full items-center gap-3 mb-4 pb-2 border-b"):
                ui.html(_CHAT_HEADER_SVG)
                ui.label("Research Assistant").classes("font-semibold text-gray-700")
                ui.label("Discuss your findings").classes("text-sm text-gray-500 ml-auto")

            self.chat_scroll = (
                ui.scroll_area()
                .classes("w-full flex-grow px-2 chat-messages-scroll")
                .style("min-height: 400px; max-height: calc(100vh - 350px);")
            )
            ui.run_javascript(_CHAT_SCROLL_OBSERVER_SCRIPT)

            self.status_container = ui.element("div").classes("hidden")
            with self.status_container:
                render_thinking_status("Analyzing your message...")

    def _play_sound(self, sound_type: str) -> None:
        safe_run_javascript(_chat_sound_script(sound_type))

    def _scroll_to_bottom(self) -> None:
        safe_run_javascript(
            """
            setTimeout(() => {
                const el = document.querySelector('.chat-messages-scroll .q-scrollarea__container');
                if (el) el.scrollTop = el.scrollHeight;
            }, 100);
            """
        )

    def _render_message_bubble(self, role: str, content: str) -> None:
        if role == "user":
            with (
                ui.row().classes("w-full justify-end mb-3"),
                ui.element("div").classes("chat-bubble-user"),
            ):
                ui.label(content).classes("text-sm")
            return

        with ui.row().classes("items-start gap-2 mb-3"):
            ui.html(_CHAT_AVATAR_HTML)
            with ui.element("div").classes("chat-bubble-assistant"):
                ui.markdown(content).classes("text-sm")

    def _render_empty_state(self) -> None:
        with ui.column().classes("w-full items-center py-8"):
            ui.icon("chat_bubble_outline", size="xl").classes("text-gray-300 mb-4")
            if self.context.can_edit:
                ui.label("Start a conversation").classes("text-lg font-medium text-gray-600")
                ui.label("Ask questions about your research findings").classes(
                    "text-sm text-gray-400 mb-4"
                )
                with ui.column().classes("gap-2"):
                    for suggestion in [
                        "What are the main findings?",
                        "How strong is the evidence?",
                        "What should I investigate next?",
                    ]:
                        ui.button(
                            suggestion,
                            on_click=lambda s=suggestion: self._quick_send(s),
                        ).props("flat dense").classes("text-indigo-600 normal-case")
                return
            ui.label("No messages yet").classes("text-lg font-medium text-gray-600")
            ui.label("You have view-only access to this job.").classes("text-sm text-gray-400")

    async def _load_chat_messages(self) -> list[Any]:
        async with get_session_ctx() as session:
            await set_current_user(session, UUID(self.context.user_id))
            return await get_chat_history(session, self.job_uuid)

    async def _render_messages(self) -> None:
        guard = ClientGuard()
        if not guard.is_connected or self.chat_scroll is None:
            return

        try:
            messages = await self._load_chat_messages()
            if not guard.is_connected:
                return

            self.chat_scroll.clear()
            with self.chat_scroll:
                if not messages:
                    self._render_empty_state()
                else:
                    for message in messages:
                        self._render_message_bubble(message.role, message.content)
            self._scroll_to_bottom()
        except Exception as exc:
            logger.error("Failed to load chat history: %s", exc)

    def _toggle_typing_indicator(self, visible: bool) -> None:
        if self.status_container is None:
            return
        if visible:
            self.status_container.classes(remove="hidden")
            return
        self.status_container.classes(add="hidden")

    def _read_input_message(self) -> str | None:
        if self.chat_input is None:
            return None
        message = (self.chat_input.value or "").strip()
        return message or None

    def _clear_input(self, guard: ClientGuard) -> None:
        if self.chat_input is None or self.send_btn is None:
            return
        self.chat_input.value = ""
        guard.run_javascript(
            "document.querySelector('textarea[placeholder=\"Ask about your research...\"]').value = ''"
        )
        self.send_btn.disable()

    async def _send_message_to_backend(self, message: str) -> None:
        async with get_session_ctx() as session:
            await set_current_user(session, UUID(self.context.user_id))
            await send_chat_message(session, self.job_uuid, message, self.context.job_dir)

    def _restore_input(self, guard: ClientGuard) -> None:
        if not guard.is_connected or self.send_btn is None or self.chat_input is None:
            return
        self.send_btn.enable()
        self.chat_input.run_method("focus")

    async def _send_message(self) -> None:
        guard = ClientGuard()
        if not guard.is_connected or self.chat_scroll is None:
            return

        message = self._read_input_message()
        if not message:
            return

        self._play_sound("sound-send")
        self._clear_input(guard)
        with self.chat_scroll:
            self._render_message_bubble("user", message)
        self._toggle_typing_indicator(True)
        self._scroll_to_bottom()

        try:
            await self._send_message_to_backend(message)
            if not guard.is_connected:
                return
            self._toggle_typing_indicator(False)
            self._play_sound("sound-receive")
            await self._render_messages()
        except Exception as exc:
            logger.error("Chat error: %s", exc, exc_info=True)
            if guard.is_connected:
                self._toggle_typing_indicator(False)
                self._play_sound("sound-error")
                ui.notify("An error occurred. Please try again.", type="negative")
        finally:
            self._restore_input(guard)

    async def _quick_send(self, message: str) -> None:
        if self.chat_input is None:
            return
        self.chat_input.value = message
        await self._send_message()

    def _render_input_area(self) -> None:
        with ui.row().classes("w-full max-w-3xl mx-auto gap-3 mt-4 chat-input-row"):
            with ui.element("div").classes("flex-grow chat-input-container flex items-center px-4"):
                self.chat_input = (
                    ui.textarea(placeholder="Ask about your research...")
                    .classes("flex-grow")
                    .props("borderless dense rows=1 autogrow input-class='text-sm py-3'")
                )

            self.send_btn = (
                ui.button(icon="send")
                .props("round color=indigo size=md")
                .classes("shadow-lg chat-send-btn")
            )

        self.send_btn.on_click(self._send_message)
        self.chat_input.on(
            "keydown.enter",
            lambda e: self._send_message() if not e.args.get("shiftKey") else None,
        )


def _render_chat_tab(context: _JobDetailContext) -> None:
    _ChatTabController(context).render()


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
