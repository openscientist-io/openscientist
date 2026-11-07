"""Analysis log rendering for the job detail timeline."""

import asyncio
from dataclasses import dataclass
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
                output_str[:RESULT_PREVIEW_CHARS]
                + ("..." if len(output_str) > RESULT_PREVIEW_CHARS else ""),
                language="text",
            ).classes("text-xs")
    elif not success:
        ui.label(output_str).classes("text-xs text-red-600 mt-1")
    else:
        ui.label(output_str).classes(meta)


def _render_action_card(entry: dict[str, Any]) -> None:
    success = entry.get("success", True)
    action_type = entry.get("action", "")
    description = entry.get("description", action_type or "Unknown")
    status_icon = "\u2705" if success else "\u274c"

    with ui.card().classes(_action_card_class(action_type)):
        with ui.row().classes("items-center gap-2"):
            ui.label(f"{status_icon} {description}").classes("font-medium text-sm")
            ui.badge(action_type, color="gray").props("outline").classes("text-xs")
        _render_analysis_log_details(entry, success)


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
) -> None:
    """The iteration's ``total`` actions, ``first`` shown and the rest a
    button press away, with ``state`` tracking both so the poll can move the
    total without rebuilding the shown cards."""
    if not total:
        return
    with box:
        with ui.expansion(f"Actions ({total})", icon="build").classes("w-full") as expansion:
            with ui.column().classes("w-full gap-0") as cards:
                await _render_action_cards(cards, first)
    state.expansion = expansion
    state.cards = cards
    state.total = total
    state.shown = len(first)
    _ensure_show_more(state, log)
