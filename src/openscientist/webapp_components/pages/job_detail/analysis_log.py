"""Analysis log rendering for the job detail timeline."""

from dataclasses import dataclass
from typing import Any

from nicegui import ui


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
