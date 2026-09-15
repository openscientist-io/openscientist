"""Timeline, iteration, statistics, plots, literature, and hypothesis rendering
for the job detail page.

Renders the "Investigation Timeline" section: per-iteration cards (analysis
log actions, plots, literature, hypotheses, findings), the job stats badges,
and the research question card. Consumed by `_render_timeline_tab` in
`job_detail.py`, which remains the orchestration seam between timeline,
feedback, and polling.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nicegui import ui

from openscientist.agent.factory import agent_class_for_provider_id
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail_context import _JobDetailContext
from openscientist.webapp_components.ui_components import (
    STATUS_COLORS,
    render_job_action_buttons,
    render_justified_text,
    render_pmid_badge,
    render_stat_badges,
    render_text_with_pmid_links,
    render_thinking_status,
)
from openscientist.webapp_components.utils import is_client_connected


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


def _timeline_header_text(
    strapline: str,
    summary_text: str,
    is_in_progress: bool,
    has_activity: bool = False,
) -> str:
    if strapline:
        base_text = strapline
    elif summary_text:
        base_text = summary_text[:80] + "..." if len(summary_text) > 80 else summary_text
    elif is_in_progress:
        return "Investigation in progress..."
    elif has_activity:
        # The model did work this iteration (code/searches/findings are shown
        # below) but never called save_iteration_summary. Make that explicit so
        # the row is not misread as the iteration having done nothing.
        return "Activity logged, but no summary recorded"
    else:
        # No recorded activity and no summary. Say that plainly rather than
        # "Completed", which misleadingly read as the model declaring the whole
        # investigation done.
        return "No activity or summary recorded"
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
    has_activity = bool(code_count or search_count or finding_count or hypothesis_count)
    header_text = _timeline_header_text(strapline, summary_text, is_in_progress, has_activity)

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
