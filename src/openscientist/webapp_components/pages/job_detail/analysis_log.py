"""Analysis log rendering for the job detail timeline.

Every action renders as one dense row with a type icon, a status dot, a
one-line description and the time and duration at the right. Arguments,
code and output live in the row's details, collapsed by default with
capped, scrollable blocks, and short single-line output shows in the row
itself.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from nicegui import ui

from openscientist.database.session import thread_safe_user_session
from openscientist.knowledge_state_db import load_iteration_analysis_log

#: How much of an action's result the page shows.
RESULT_PREVIEW_CHARS = 2000

#: How many action cards an opened iteration shows at a time.
ACTIONS_PER_BATCH = 50

#: How many action cards one render chunk creates before yielding to the loop.
CARDS_PER_CHUNK = 10

#: Output shorter than this and on one line fits the row without details.
_INLINE_OUTPUT_MAX_CHARS = 160

#: The rows' styles, added to the job page's head with the timeline. The
#: plain and expandable rows share one grid: the icon sits after the 10px
#: header padding, the facts end 32px from the row's right edge, and the
#: last 24px column holds the expand arrow (or stays empty on plain rows).
ACTION_STYLES = """
<style>
.os-action { border-left: 3px solid #d1d5db; margin-bottom: 4px; background: #fff;
  min-width: 0; }
.os-action-execute { border-left-color: #93c5fd; }
.os-action-search { border-left-color: #c4b5fd; }
.os-action-knowledge { border-left-color: #86efac; }
.os-action-header { padding: 2px 8px 2px 10px; }
.os-action-plain .os-action-header { padding-right: 32px; }
.os-action-icon { font-size: 16px; width: 18px; color: #6b7280; flex: none; }
.os-action-dot { width: 6px; height: 6px; border-radius: 50%; background: #22c55e; flex: none; }
.os-action-dot-failed { background: #ef4444; }
.os-action-title { font-size: 13px; min-width: 0; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.os-action-title-failed { color: #dc2626; }
.os-action-facts { font-size: 12px; color: #6b7280; flex: none; }
.os-action-inline { font-size: 12px; color: #4b5563; padding: 0 10px 4px 46px; margin: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.os-action-inline-failed { color: #dc2626; }
.os-action.q-expansion-item .os-action-header-col { flex: 1 1 0%; min-width: 0; }
.os-action .q-expansion-item__container > .q-item { padding: 0; min-height: 0; }
.os-action .q-expansion-item__container > .q-item > .q-item__section--side {
  padding: 0; width: 24px; flex: none; }
.os-action .q-expansion-item__content { padding: 2px 10px 8px 34px; }
.os-action-meta { font-size: 12px; color: #4b5563; margin: 2px 0; }
.os-actions { min-width: 0; }
.os-action .nicegui-code { width: 100%; max-height: 12rem; overflow: auto; margin: 4px 0; }
.os-action .nicegui-code pre { font-size: 12px; }
</style>
"""


@dataclass(frozen=True)
class _IterationLog:
    """One iteration's analysis log, read as the page's viewer."""

    job_id: str
    user_id: str
    iteration: int

    async def actions(self, offset: int) -> list[dict[str, Any]]:
        """The batch of actions that starts at ``offset``, oldest first."""
        return await self._read(offset=offset, limit=ACTIONS_PER_BATCH)

    async def searches(self) -> list[dict[str, Any]]:
        """Every literature search of the iteration."""
        return await self._read(action_type="search_pubmed")

    async def _read(self, **selection: Any) -> list[dict[str, Any]]:
        async with thread_safe_user_session(UUID(self.user_id)) as session:
            return await load_iteration_analysis_log(
                session,
                UUID(self.job_id),
                self.iteration,
                output_chars=RESULT_PREVIEW_CHARS,
                **selection,
            )


#: The action types the row's icon knows, as the tool name contains them.
_ACTION_ICONS: tuple[tuple[str, str], ...] = (
    ("execute_code", "code"),
    ("search_pubmed", "search"),
    ("update_knowledge_state", "bolt"),
    ("add_hypothesis", "psychology"),
    ("update_hypothesis", "psychology"),
    ("read_document", "description"),
    ("set_status", "info"),
    ("set_job_title", "title"),
    ("save_iteration_summary", "summary"),
    ("run_phenix_tool", "science"),
    ("compare_structures", "compare_arrows"),
    ("parse_alphafold_confidence", "insights"),
)


def _action_icon(action_type: str) -> str:
    for needle, icon in _ACTION_ICONS:
        if needle in action_type:
            return icon
    return "more_horiz"


def _action_kind(action_type: str) -> str:
    if "execute_code" in action_type:
        return "execute"
    if "search_pubmed" in action_type:
        return "search"
    if "update_knowledge_state" in action_type:
        return "knowledge"
    return "other"


def _action_facts(entry: dict[str, Any]) -> str | None:
    """The time and duration at the row's right, if either is known."""
    parts: list[str] = []
    stamp = entry.get("timestamp")
    if isinstance(stamp, str):
        parts.append(datetime.fromisoformat(stamp).strftime("%H:%M:%S"))
    duration = entry.get("execution_time")
    if isinstance(duration, (int, float)):
        parts.append(f"{duration:.1f}s")
    return " \u00b7 ".join(parts) if parts else None


def _inline_output(entry: dict[str, Any]) -> str | None:
    """The output when it is short and single-line, so it fits the row."""
    output = entry.get("output")
    if output is None:
        return None
    text = str(output)
    if len(text) > _INLINE_OUTPUT_MAX_CHARS or "\n" in text:
        return None
    return text


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
        if hypothesis_id := entry.get("hypothesis_id"):
            lines.append(_AnalysisLogMetaLine(f"Hypothesis: {hypothesis_id}"))
        if statement := entry.get("statement"):
            lines.append(_AnalysisLogMetaLine(str(statement), italic=True))
        if status := entry.get("status"):
            lines.append(_AnalysisLogMetaLine(f"Status: {status}"))
        if summary := entry.get("result_summary"):
            lines.append(_AnalysisLogMetaLine(str(summary)))
    elif "update_knowledge_state" in action_type:
        if title := entry.get("title"):
            lines.append(_AnalysisLogMetaLine(f"Finding: {title}"))
        if evidence := entry.get("evidence"):
            lines.append(_AnalysisLogMetaLine(f"Evidence: {evidence}"))
    elif "read_document" in action_type:
        if file_path := entry.get("file_path"):
            lines.append(_AnalysisLogMetaLine(str(file_path)))
    elif "run_phenix_tool" in action_type:
        if tool_name := entry.get("tool_name"):
            lines.append(_AnalysisLogMetaLine(f"Tool: {tool_name}"))

    language = entry.get("language")
    if "execute_code" in action_type and isinstance(language, str) and language != "python":
        lines.append(_AnalysisLogMetaLine(f"Language: {language}"))

    return lines


def _render_action_header(entry: dict[str, Any]) -> None:
    success = entry.get("success", True)
    action_type = entry.get("action", "")
    description = entry.get("description") or action_type or "Unknown"
    with ui.row().classes("os-action-header w-full no-wrap items-center gap-1.5"):
        icon = ui.icon(_action_icon(action_type)).classes("os-action-icon")
        with icon:
            ui.tooltip(action_type)
        dot = ui.element("span").classes("os-action-dot")
        if not success:
            dot.classes(add="os-action-dot-failed")
        title = ui.label(str(description)).classes("os-action-title flex-1")
        if not success:
            title.classes(add="os-action-title-failed")
        if facts := _action_facts(entry):
            ui.label(facts).classes("os-action-facts")


def _render_inline_output(text: str, success: bool) -> None:
    label = ui.label(text).classes("os-action-inline")
    if not success:
        label.classes(add="os-action-inline-failed")


def _render_action_details(entry: dict[str, Any], inline: str | None) -> None:
    action_type = entry.get("action", "")

    for line in _analysis_log_meta_lines(entry):
        label = ui.label(line.text).classes("os-action-meta")
        if line.italic:
            label.style("font-style: italic")

    code = entry.get("code")
    if code and "execute_code" in action_type:
        language = entry.get("language")
        ui.code(str(code), language=language if isinstance(language, str) else "python").classes(
            "os-action-code"
        )

    output = entry.get("output")
    if output is None or inline is not None:
        return
    output_str = str(output)
    ui.code(
        output_str[:RESULT_PREVIEW_CHARS]
        + ("..." if len(output_str) > RESULT_PREVIEW_CHARS else ""),
        language="text",
    ).classes("os-action-output")


def _render_action_card(entry: dict[str, Any]) -> None:
    """One dense row for the action, its details behind an expand when it
    has any, and its short output inline."""
    success = entry.get("success", True)
    action_type = entry.get("action", "")
    kind = _action_kind(action_type)
    inline = _inline_output(entry)
    has_code = "execute_code" in action_type and bool(entry.get("code"))
    has_details = (
        bool(_analysis_log_meta_lines(entry))
        or has_code
        or (inline is None and entry.get("output") is not None)
    )

    classes = f"os-action os-action-{kind}"
    if not has_details:
        with ui.column().classes(f"{classes} os-action-plain w-full gap-0"):
            _render_action_header(entry)
            if inline is not None:
                _render_inline_output(inline, success)
        return

    with ui.expansion().classes(f"{classes} w-full").props("dense") as action:
        with action.add_slot("header"):
            with ui.column().classes("os-action-header-col gap-0"):
                _render_action_header(entry)
                if inline is not None:
                    _render_inline_output(inline, success)
        with action:
            _render_action_details(entry, inline)


async def _render_action_cards(cards: ui.column, entries: list[dict[str, Any]]) -> None:
    """Render ``entries`` under ``cards`` a few cards at a time, yielding to
    the event loop between chunks so a batch does not hold the loop."""
    for start in range(0, len(entries), CARDS_PER_CHUNK):
        if cards.is_deleted:
            return
        with cards:
            for entry in entries[start : start + CARDS_PER_CHUNK]:
                _render_action_card(entry)
        await asyncio.sleep(0)


@dataclass
class _ActionsState:
    """How many of the iteration's actions the page shows, and the elements
    that offer the rest, so the poll can move the total in place."""

    shown: int = 0
    total: int = 0
    expansion: ui.expansion | None = None
    cards: ui.column | None = None
    button: ui.button | None = None


def _ensure_show_more(state: _ActionsState, log: _IterationLog) -> None:
    """The button that appends the next batch of actions, offered while fewer
    than ``state.total`` are shown."""
    if state.button is not None or state.cards is None or state.expansion is None:
        return
    if state.cards.is_deleted or state.expansion.is_deleted or state.shown >= state.total:
        return

    async def show_more() -> None:
        if state.button is None or not state.button.enabled:
            return
        state.button.disable()
        try:
            batch = await log.actions(state.shown)
        finally:
            if state.button is not None and not state.button.is_deleted:
                state.button.enable()
        if state.cards is None or state.cards.is_deleted:
            return
        state.shown += len(batch)
        await _render_action_cards(state.cards, batch)
        if not batch or state.shown >= state.total:
            state.button.delete()
            state.button = None

    with state.expansion:
        state.button = ui.button("Show more", on_click=show_more).props("flat dense")


async def _render_analysis_log_actions(
    box: ui.column,
    total: int,
    first: list[dict[str, Any]],
    log: _IterationLog,
    state: _ActionsState,
    *,
    direct_access: bool,
) -> None:
    """The iteration's ``total`` actions, ``first`` shown and the rest a
    button press away, with ``state`` tracking both so the poll can move the
    total without rebuilding the shown cards."""
    if not total:
        return
    with box:
        with ui.expansion(f"Actions ({total})", icon="build").classes("w-full") as expansion:
            if not direct_access:
                ui.label(
                    "Action output is shown only to the job's owner and the people it is shared with."
                ).classes("text-sm text-gray-500")
            with ui.column().classes("os-actions w-full gap-0") as cards:
                await _render_action_cards(cards, first)
    state.expansion = expansion
    state.cards = cards
    state.total = total
    state.shown = len(first)
    _ensure_show_more(state, log)
