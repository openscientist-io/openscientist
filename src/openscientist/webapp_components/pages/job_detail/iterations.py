"""Iteration card rendering for the job detail timeline.

Each iteration card is built once and updated in place, so its expansion
state, its loaded action batches and the page's scroll position survive the
poll.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nicegui import ui
from nicegui.events import ValueChangeEventArguments

from openscientist.artifact_packager import MAX_ARTIFACT_FILE_SIZE_BYTES
from openscientist.job.types import JobStatus
from openscientist.job_files import (
    job_file_exists,
    list_job_files,
    read_job_file_bytes,
    read_job_file_text,
)
from openscientist.webapp_components.components.badges import (
    render_pmid_badge,
    render_text_with_pmid_links,
)
from openscientist.webapp_components.components.banners import render_justified_text
from openscientist.webapp_components.pages.job_detail.analysis_log import (
    _ActionsState,
    _ensure_show_more,
    _IterationLog,
    _render_analysis_log_actions,
)
from openscientist.webapp_components.pages.job_detail.common import (
    _JobDetailContext,
    _PageSnapshot,
)
from openscientist.webapp_components.utils import (
    is_client_connected,
)

#: An iteration card's body starts directly under its header, its sections
#: sit flush against each other, the timeline's cards sit a three-quarters
#: rem apart, the sections' header rows are compact 32px rows, and the
#: scroll content never grows past the pane, so the page keeps no empty
#: room between them and a wide code line cannot push the timeline sideways.
ITERATION_STYLES = """
<style>
.os-iteration-card > .q-expansion-item__container > .q-expansion-item__content > .nicegui-expansion-content {
  padding-top: 0;
  padding-bottom: 0;
}
.os-iteration-body { gap: 0; }
.os-iteration-body > .nicegui-column:empty { display: none; }
.os-iteration-timeline .q-scrollarea__content { gap: 0.75rem; width: 100%; }
.os-iteration-timeline .q-scrollarea__content * { min-width: 0; }
.os-iteration-timeline .nicegui-code { max-width: 100%; }
.os-iteration-timeline .nicegui-code pre { max-width: 100%; overflow-x: auto; }
.os-iteration-body > .nicegui-column > .q-expansion-item > .q-expansion-item__container > .q-item {
  min-height: 32px;
  padding: 2px 16px;
}
.os-iteration-body > .nicegui-column > .q-expansion-item > .q-expansion-item__container > .q-item > .q-item__section--avatar {
  min-width: 0;
  padding-right: 8px;
}
.os-iteration-body > .nicegui-column > .q-expansion-item > .q-expansion-item__container > .q-item > .q-item__section--avatar > .q-icon {
  font-size: 20px;
}
</style>
"""


def _show_no_timeline_activity() -> ui.label:
    return ui.label("No investigation activity yet").classes("text-gray-500")


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


def _normalize_iteration_summary(iter_summary: Any) -> tuple[str, str]:
    if isinstance(iter_summary, str):
        return "", iter_summary
    if not isinstance(iter_summary, dict):
        return "", ""
    return iter_summary.get("strapline", ""), iter_summary.get("summary", "")


def _iteration_activity_counts(action_counts: dict[str, int]) -> tuple[int, int, int]:
    code_count = action_counts.get("execute_code", 0)
    search_count = action_counts.get("search_pubmed", 0)
    finding_count = action_counts.get("update_knowledge_state", 0)
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
    """This iteration's plots with metadata. The job directory is
    agent-writable, so the plot listing and metadata reads go through the
    symlink-safe walk and links the agent plants are invisible."""
    job_dir = iter_provenance_dir.parent
    plots: list[tuple[Path, dict[str, Any]]] = []
    for name in list_job_files(job_dir, iter_provenance_dir.name):
        if not name.endswith(".png"):
            continue
        metadata_name = Path(name).with_suffix(".json").name
        metadata_text = read_job_file_text(
            job_dir,
            f"{iter_provenance_dir.name}/{metadata_name}",
            max_size=MAX_ARTIFACT_FILE_SIZE_BYTES,
        )
        if metadata_text is None:
            continue
        try:
            metadata = json.loads(metadata_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(metadata, dict) or metadata.get("iteration") != iter_num:
            continue
        plots.append((iter_provenance_dir / name, metadata))
    return plots


def _job_file_url(job_dir: Path, path: Path) -> str:
    """The URL that serves ``path``, a file of the job in ``job_dir``, to users
    who may see the job."""
    return f"/web/jobs/{job_dir.name}/files/{quote(path.relative_to(job_dir).as_posix())}"


def _download_plot(job_dir: Path, relative: str) -> None:
    """Serve a plot at click time from the inode the walk opens, so a link the
    agent plants between render and click cannot redirect the download."""
    data = read_job_file_bytes(job_dir, relative, max_size=MAX_ARTIFACT_FILE_SIZE_BYTES)
    if data is None:
        if job_file_exists(job_dir, relative):
            ui.notify("The plot is too large to download.", type="warning")
        else:
            ui.notify("The plot is no longer available.", type="warning")
        return
    ui.download(data, filename=Path(relative).name)


def _render_iteration_plots(iter_provenance_dir: Path, iter_num: int) -> None:
    iteration_plots = _collect_iteration_plots(iter_provenance_dir, iter_num)
    if not iteration_plots:
        return

    with (
        ui.expansion(
            f"Visualizations ({len(iteration_plots)})",
            icon="insert_chart",
        ).classes("w-full"),
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
                    on_click=lambda f=plot_file: _download_plot(
                        iter_provenance_dir.parent,
                        f"{iter_provenance_dir.name}/{f.name}",
                    ),
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
    literature_entries: list[Any], iter_ks_data: dict[str, Any], iter_num: int
) -> None:
    """The iteration's literature searches and the papers they retrieved."""
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
    ).classes("w-full"):
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


def _iteration_hypotheses(iter_ks_data: dict[str, Any], iter_num: int) -> list[dict[str, Any]]:
    """The hypotheses proposed or tested in one iteration, in knowledge-state
    order."""
    return [
        hypothesis
        for hypothesis in iter_ks_data.get("hypotheses", [])
        if hypothesis.get("iteration_proposed") == iter_num
        or hypothesis.get("iteration_tested") == iter_num
    ]


def _iteration_findings(iter_ks_data: dict[str, Any], iter_num: int) -> list[dict[str, Any]]:
    return [
        finding
        for finding in iter_ks_data.get("findings", [])
        if finding.get("iteration_discovered") == iter_num
    ]


def _rows_without_id(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """The rows for a section's model, without the ids the load assigns by
    position, so a reload that renumbers them does not read as a change."""
    return tuple({key: value for key, value in row.items() if key != "id"} for row in rows)


def _render_iteration_hypotheses(iter_ks_data: dict[str, Any], iter_num: int) -> None:
    iter_hypotheses = _iteration_hypotheses(iter_ks_data, iter_num)
    if not iter_hypotheses:
        return

    with ui.expansion(f"Hypotheses ({len(iter_hypotheses)})", icon="science").classes("w-full"):
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
    iteration_findings = _iteration_findings(iter_ks_data, iter_num)
    if not iteration_findings:
        return

    with ui.expansion(f"Findings ({len(iteration_findings)})", icon="lightbulb").classes("w-full"):
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


def _render_summary_section(summary_text: str) -> None:
    if not summary_text:
        return
    with ui.expansion("Summary", icon="summarize", value=True).classes("w-full"):
        render_text_with_pmid_links(summary_text, text_classes="text-sm text-gray-700")


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


@dataclass(frozen=True)
class _HeaderModel:
    """What a card's header was last rendered from."""

    text: str
    hypothesis_count: int
    code_count: int
    search_count: int
    finding_count: int
    in_progress: bool
    border_class: str


@dataclass(frozen=True)
class _BodyModel:
    """What a card's body was last rendered from, one entry per section."""

    summary_text: str
    hypotheses: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]
    plot_names: tuple[str, ...]
    search_count: int
    action_total: int


def _header_model(iteration: int, context: _JobDetailContext) -> _HeaderModel:
    ks = context.ks_data or {}
    strapline, summary_text = _normalize_iteration_summary(
        _timeline_iteration_summaries(ks).get(iteration, {})
    )
    code_count, search_count, finding_count = _iteration_activity_counts(
        context.action_counts.get(iteration, {})
    )
    hypothesis_count = len(_iteration_hypotheses(ks, iteration))
    is_in_progress = (
        iteration == ks.get("iteration", 1) and context.job_info.status == JobStatus.RUNNING
    )
    has_activity = bool(code_count or search_count or finding_count or hypothesis_count)
    return _HeaderModel(
        text=_timeline_header_text(strapline, summary_text, is_in_progress, has_activity),
        hypothesis_count=hypothesis_count,
        code_count=code_count,
        search_count=search_count,
        finding_count=finding_count,
        in_progress=is_in_progress,
        border_class=_timeline_border_class(code_count, search_count, finding_count),
    )


def _iteration_plot_names(job_dir: Path, iteration: int) -> tuple[str, ...]:
    """The plot files of one iteration, by name, read through the
    symlink-safe walk as the section renders them."""
    return tuple(
        plot_file.name
        for plot_file, _metadata in _collect_iteration_plots(job_dir / "provenance", iteration)
    )


def _body_model(
    iteration: int,
    context: _JobDetailContext,
    reuse: _BodyModel | None,
    plots_changed: bool,
) -> _BodyModel:
    """The body of one iteration from the page's current data. The plot names
    come from disk only when the job's plot files changed."""
    ks = context.ks_data or {}
    _, summary_text = _normalize_iteration_summary(
        _timeline_iteration_summaries(ks).get(iteration, {})
    )
    counts = context.action_counts.get(iteration, {})
    plot_names = (
        _iteration_plot_names(context.job_dir, iteration)
        if reuse is None or plots_changed
        else reuse.plot_names
    )
    return _BodyModel(
        summary_text=summary_text,
        hypotheses=_rows_without_id(_iteration_hypotheses(ks, iteration)),
        findings=_rows_without_id(_iteration_findings(ks, iteration)),
        plot_names=plot_names,
        search_count=counts.get("search_pubmed", 0),
        action_total=sum(counts.values()),
    )


class _IterationCard:
    """One iteration's card: built once, updated in place so its expansion
    state, its loaded batches and the page's scroll survive the poll."""

    def __init__(self, iteration: int, context: _JobDetailContext) -> None:
        self.iteration = iteration
        self.context = context
        self.log = _IterationLog(context.job_id, context.user_id, iteration)
        self.expansion: ui.expansion | None = None
        self.header_box: ui.column | None = None
        self.content_box: ui.column | None = None
        self.header_model: _HeaderModel | None = None
        self.body_model: _BodyModel | None = None
        self.body_loaded = False
        self.body_stale = False
        self.sections: dict[str, ui.column] = {}
        self.actions = _ActionsState()
        self.border_class = ""

    def build(self) -> None:
        model = _header_model(self.iteration, self.context)
        self.header_model = model
        self.border_class = model.border_class
        with ui.expansion(icon="science").classes(
            f"w-full os-iteration-card {model.border_class}"
        ) as expansion:
            with expansion.add_slot("header"):
                self.header_box = ui.column().classes("w-full")
                with self.header_box:
                    _render_iteration_header(
                        self.iteration,
                        model.text,
                        model.code_count,
                        model.search_count,
                        model.finding_count,
                        model.hypothesis_count,
                    )
            self.content_box = ui.column().classes("w-full os-iteration-body")
            with self.content_box:
                ui.label("Click to load details...").classes("text-sm text-gray-400 italic")
            expansion.on_value_change(self._on_value_change)
        self.expansion = expansion

    def _on_value_change(self, e: ValueChangeEventArguments[bool]) -> Awaitable[None] | None:
        if e.value and (not self.body_loaded or self.body_stale):
            return self._load_body()
        return None

    async def _load_body(self) -> None:
        """Load the body once, or again when a tick marked it stale. The
        flags are set before the awaits so a close and reopen while the load
        is pending does not load it twice."""
        if self.body_loaded and not self.body_stale:
            return
        self.body_loaded = True
        self.body_stale = False
        try:
            first_actions = await self.log.actions(0)
            searches = await self.log.searches()
        except BaseException:
            self.body_loaded = False
            raise
        if self.content_box is None or self.content_box.is_deleted:
            return
        await self._fill_body(first_actions, searches)

    async def _fill_body(
        self, first_actions: list[dict[str, Any]], searches: list[dict[str, Any]]
    ) -> None:
        self.body_model = _body_model(self.iteration, self.context, None, True)
        content_box = self.content_box
        if content_box is None:
            return
        content_box.clear()
        self.sections = {}
        self.actions = _ActionsState()
        with content_box:
            for name in ("summary", "hypotheses", "findings", "actions", "plots", "literature"):
                self.sections[name] = ui.column().classes("w-full")
        ks = self.context.ks_data or {}
        summary_box = self.sections["summary"]
        hypotheses_box = self.sections["hypotheses"]
        findings_box = self.sections["findings"]
        actions_box = self.sections["actions"]
        plots_box = self.sections["plots"]
        literature_box = self.sections["literature"]
        with summary_box:
            _render_summary_section(self.body_model.summary_text)
        with hypotheses_box:
            _render_iteration_hypotheses(ks, self.iteration)
        with findings_box:
            _render_iteration_findings(ks, self.iteration)
        await _render_analysis_log_actions(
            actions_box, self.body_model.action_total, first_actions, self.log, self.actions
        )
        if content_box.is_deleted:
            return
        with plots_box:
            _render_iteration_plots(self.context.job_dir / "provenance", self.iteration)
        with literature_box:
            _render_iteration_literature(searches, ks, self.iteration)

    def update_header(self, model: _HeaderModel) -> None:
        """Re-render the header row in place, moving the border when it
        changes. The expansion itself is untouched, so it stays open."""
        if self.header_box is None or self.header_box.is_deleted:
            return
        self.header_box.clear()
        with self.header_box:
            _render_iteration_header(
                self.iteration,
                model.text,
                model.code_count,
                model.search_count,
                model.finding_count,
                model.hypothesis_count,
            )
        if model.border_class != self.border_class and self.expansion is not None:
            self.expansion.classes(remove=self.border_class, add=model.border_class)
            self.border_class = model.border_class
        self.header_model = model

    async def _update_actions(self, total: int) -> None:
        """The Actions total and its Show more offer, without touching the
        shown cards. A body the first load built without log rows skips the
        section, which a tick creates from the log once actions exist."""
        actions = self.actions
        actions.total = total
        if actions.expansion is None:
            if not total:
                return
            box = self.sections.get("actions")
            if box is None:
                return
            first_actions = await self.log.actions(0)
            if box.is_deleted:
                return
            await _render_analysis_log_actions(box, total, first_actions, self.log, actions)
            return
        if not actions.expansion.is_deleted:
            actions.expansion.text = f"Actions ({total})"
        if actions.shown >= total:
            if actions.button is not None and not actions.button.is_deleted:
                actions.button.delete()
                actions.button = None
            return
        _ensure_show_more(actions, self.log)

    def _rebuild_section(self, name: str, render: Callable[[], None]) -> None:
        """Clear one section's container and rebuild it from the new model."""
        box = self.sections.get(name)
        if box is None or box.is_deleted:
            return
        box.clear()
        with box:
            render()

    async def refresh_changed_sections(self, new_body: _BodyModel) -> None:
        """Update the sections of an open card whose model changed. Sections
        whose model did not change are not touched, and the Actions list is
        never rebuilt, only its total and its Show more offer."""
        old_body = self.body_model
        if old_body is None:
            return
        ks = self.context.ks_data or {}
        if new_body.summary_text != old_body.summary_text:
            self._rebuild_section("summary", lambda: _render_summary_section(new_body.summary_text))
        if new_body.hypotheses != old_body.hypotheses:
            self._rebuild_section(
                "hypotheses", lambda: _render_iteration_hypotheses(ks, self.iteration)
            )
        if new_body.findings != old_body.findings:
            self._rebuild_section(
                "findings", lambda: _render_iteration_findings(ks, self.iteration)
            )
        if new_body.action_total != old_body.action_total:
            await self._update_actions(new_body.action_total)
        if new_body.plot_names != old_body.plot_names:
            provenance = self.context.job_dir / "provenance"
            self._rebuild_section(
                "plots", lambda: _render_iteration_plots(provenance, self.iteration)
            )
        if new_body.search_count != old_body.search_count:
            searches = await self.log.searches()
            self._rebuild_section(
                "literature",
                lambda: _render_iteration_literature(searches, ks, self.iteration),
            )
        self.body_model = new_body


class _Timeline:
    """The page's investigation timeline: the cards live for the page's life
    and the poll applies each change to the cards that own it."""

    def __init__(self, context: _JobDetailContext) -> None:
        self.context = context
        self.cards: dict[int, _IterationCard] = {}
        self.container: ui.scroll_area | None = None
        self.no_activity: ui.label | None = None

    def _display_max(self) -> int:
        """The last iteration the page shows, one short while awaiting
        feedback."""
        display_max = int((self.context.ks_data or {}).get("iteration", 1))
        if self.context.job_info.status == JobStatus.AWAITING_FEEDBACK:
            display_max -= 1
        return display_max

    def build_initial(self) -> None:
        if not is_client_connected():
            return
        ks = self.context.ks_data
        if not ks or (not self.context.action_counts and not _timeline_iteration_summaries(ks)):
            self.no_activity = _show_no_timeline_activity()
            return
        container = self._ensure_card_container()
        with container:
            for iteration in range(1, self._display_max() + 1):
                self._ensure_card(iteration)

    def _ensure_card_container(self) -> ui.scroll_area:
        if self.container is None:
            self.container = ui.scroll_area().classes("w-full h-[600px] os-iteration-timeline")
        return self.container

    def _ensure_card(self, iteration: int) -> None:
        if iteration in self.cards:
            return
        if self.no_activity is not None:
            self.no_activity.delete()
            self.no_activity = None
        with self._ensure_card_container():
            card = _IterationCard(iteration, self.context)
            self.cards[iteration] = card
            card.build()

    async def update(self, old_snapshot: _PageSnapshot | None, snapshot: _PageSnapshot) -> None:
        """Apply one changed tick: a card appears once its iteration has a
        summary or a log row, headers update in place, the sections of an open
        card update in place and a closed body is marked stale for its next
        open."""
        if not is_client_connected():
            return
        ks = self.context.ks_data
        if not ks:
            return
        summaries = _timeline_iteration_summaries(ks)
        for iteration in range(1, self._display_max() + 1):
            if iteration not in self.cards and (
                self.context.action_counts.get(iteration) or iteration in summaries
            ):
                self._ensure_card(iteration)
        if self.container is None:
            return
        plots_changed = old_snapshot is None or snapshot.plot_names != old_snapshot.plot_names
        for card in self.cards.values():
            if card.expansion is None or card.expansion.is_deleted:
                continue
            new_header = _header_model(card.iteration, self.context)
            if new_header != card.header_model:
                card.update_header(new_header)
            if card.body_loaded and card.body_model is not None:
                new_body = _body_model(card.iteration, self.context, card.body_model, plots_changed)
                if new_body != card.body_model:
                    if card.expansion.value:
                        await card.refresh_changed_sections(new_body)
                    else:
                        card.body_stale = True
