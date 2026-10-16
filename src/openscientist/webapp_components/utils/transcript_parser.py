"""Transcript parsing utilities for the web application."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openscientist.transcript import ToolCall, ToolResult, TranscriptEntry

# Known OpenScientist tool names (bare names, without MCP server prefix).
_OPENSCIENTIST_TOOL_NAMES = frozenset(
    {
        "execute_code",
        "search_pubmed",
        "update_knowledge_state",
        "add_hypothesis",
        "update_hypothesis",
        "save_iteration_summary",
        "read_document",
        "set_status",
        "set_job_title",
        "set_consensus_answer",
        "run_phenix_tool",
        "compare_structures",
        "parse_alphafold_confidence",
        "search_biomodels",
        "fetch_biomodel",
    }
)


@dataclass
class UsageSummary:
    """Summary of tools and skills used in a transcript."""

    tool_counts: dict[str, int] = field(default_factory=dict)
    skill_invocations: list[str] = field(default_factory=list)
    mcp_tool_calls: int = 0
    code_executions: int = 0
    pubmed_searches: int = 0
    findings_recorded: int = 0

    @property
    def skills_used(self) -> list[str]:
        """Deduplicated list of skills invoked."""
        return list(dict.fromkeys(self.skill_invocations))


def _short_tool_name(tool_name: str) -> str:
    """Return short tool name without MCP prefix."""
    return tool_name.split("__")[-1] if "__" in tool_name else tool_name


def _is_openscientist_tool(tool_name: str, short_name: str) -> bool:
    """Return whether a tool belongs to the OpenScientist toolset."""
    return "openscientist" in tool_name.lower() or short_name in _OPENSCIENTIST_TOOL_NAMES


def _collect_tool_results_by_call_id(
    transcript: list[TranscriptEntry],
) -> dict[str, ToolResult]:
    """Index ``ToolResult`` entries by their ``call_id``."""
    return {entry.call_id: entry for entry in transcript if isinstance(entry, ToolResult)}


def _iter_tool_calls(transcript: list[TranscriptEntry]) -> list[ToolCall]:
    """Return every ``ToolCall`` in the transcript, in source order."""
    return [entry for entry in transcript if isinstance(entry, ToolCall)]


def get_action_description(tool_call: ToolCall) -> str:
    """Return a human-readable description for a ``ToolCall``."""
    inp = tool_call.arguments

    if inp.get("description"):
        return str(inp["description"])

    name = tool_call.tool
    if "search_pubmed" in name:
        return f"Search: {inp.get('query', '')}"
    if "update_knowledge_state" in name:
        return f"Finding: {inp.get('title', '')}"
    if "save_iteration_summary" in name:
        return f"Summary: {str(inp.get('summary', ''))[:50]}..."
    if "execute_code" in name:
        return "Code execution"
    if name == "Skill":
        return f"Skill: {inp.get('skill', 'unknown')}"

    return _short_tool_name(name)


def parse_transcript_actions(transcript: list[TranscriptEntry]) -> list[dict[str, Any]]:
    """Extract OpenScientist tool actions paired with their results.

    Returns one dict per ``ToolCall`` whose tool belongs to the
    OpenScientist toolset, with the matched ``ToolResult`` (if any)
    folded in. The shape is stable for UI consumers.
    """
    actions: list[dict[str, Any]] = []
    results = _collect_tool_results_by_call_id(transcript)

    for call in _iter_tool_calls(transcript):
        short_name = _short_tool_name(call.tool)
        if not _is_openscientist_tool(call.tool, short_name):
            continue

        result = results.get(call.id)
        actions.append(
            {
                "tool_name": call.tool,
                "short_name": short_name,
                "description": get_action_description(call),
                "input": call.arguments,
                "result": result.output if result is not None else "",
                "success": result.success if result is not None else True,
            }
        )

    return actions


def extract_usage_summary(transcript: list[TranscriptEntry]) -> UsageSummary:
    """Tally tool / skill usage across the transcript."""
    summary = UsageSummary()

    for call in _iter_tool_calls(transcript):
        tool_name = call.tool
        short_name = _short_tool_name(tool_name)
        summary.tool_counts[short_name] = summary.tool_counts.get(short_name, 0) + 1

        if "execute_code" in tool_name:
            summary.code_executions += 1
            summary.mcp_tool_calls += 1
        elif "search_pubmed" in tool_name:
            summary.pubmed_searches += 1
            summary.mcp_tool_calls += 1
        elif "update_knowledge_state" in tool_name:
            summary.findings_recorded += 1
            summary.mcp_tool_calls += 1
        elif _is_openscientist_tool(tool_name, short_name):
            summary.mcp_tool_calls += 1

        if tool_name == "Skill":
            skill_name = call.arguments.get("skill", "")
            if skill_name:
                summary.skill_invocations.append(skill_name)

    return summary
