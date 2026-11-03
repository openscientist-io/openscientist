"""Iteration card rendering for the job detail timeline."""

import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nicegui import ui

from openscientist.job.types import JobStatus
from openscientist.webapp_components.components.badges import (
    render_pmid_badge,
    render_text_with_pmid_links,
)
from openscientist.webapp_components.components.banners import render_justified_text
from openscientist.webapp_components.pages.job_detail.analysis_log import (
    _render_analysis_log_actions,
)
from openscientist.webapp_components.utils import (
    is_client_connected,
)


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


def _job_file_url(job_dir: Path, path: Path) -> str:
    """The URL that serves ``path``, a file of the job in ``job_dir``, to users
    who may see the job."""
    return f"/web/jobs/{job_dir.name}/files/{quote(path.relative_to(job_dir).as_posix())}"


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
                ui.image(_job_file_url(iter_provenance_dir.parent, plot_file)).classes("w-full")
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
