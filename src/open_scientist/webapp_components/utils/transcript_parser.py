"""Transcript parsing utilities for the web application."""

import json
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

# Known Open Scientist tool names (bare names, without MCP server prefix).
# Used to identify open_scientist tools in SDK transcripts where names are not
# prefixed with "open_scientist-tools__".
_OPEN_SCIENTIST_TOOL_NAMES = frozenset(
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


def _is_open_scientist_tool(tool_name: str, short_name: str) -> bool:
    """Return whether a tool belongs to the Open Scientist toolset."""
    return "open_scientist" in tool_name.lower() or short_name in _OPEN_SCIENTIST_TOOL_NAMES


def _extract_tool_result_text(result: Any) -> str:
    """Normalize tool_result payload into text for timeline display."""
    if isinstance(result, dict):
        return str(result.get("result", str(result)))
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        return str(result)
    return ""


def _is_success_from_result_text(result_text: str) -> bool:
    """Infer success from free-form tool result text."""
    lowered = result_text.lower()
    return "failed" not in lowered and "error" not in lowered


def _collect_tool_results_by_id(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    """Collect user tool_result entries indexed by tool_use_id."""
    tool_results: dict[str, Any] = {}
    for entry in transcript:
        if entry.get("type") != "user":
            continue
        content = entry.get("message", {}).get("content", [])
        for item in content:
            if item.get("type") != "tool_result":
                continue
            tool_use_id = item.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                continue
            result_content = item.get("content", "")
            if isinstance(result_content, str) and result_content.startswith("{"):
                with suppress(json.JSONDecodeError):
                    result_content = json.loads(result_content)
            tool_results[tool_use_id] = result_content
    return tool_results


def _iter_assistant_tool_uses(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Yield all assistant tool_use content items from transcript."""
    tool_uses: list[dict[str, Any]] = []
    for entry in transcript:
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content", [])
        tool_uses.extend(item for item in content if item.get("type") == "tool_use")
    return tool_uses


def get_action_description(tool_use: dict[str, Any]) -> str:
    """
    Get description for a tool use action with fallback logic.

    Args:
        tool_use: Tool use object from transcript

    Returns:
        Description string
    """
    inp = tool_use.get("input", {})

    # 1. Explicit description
    if inp.get("description"):
        return str(inp["description"])

    # 2. Tool-specific fallback from key inputs
    name = tool_use.get("name", "")
    if "search_pubmed" in name:
        return f"Search: {inp.get('query', '')}"
    if "update_knowledge_state" in name:
        return f"Finding: {inp.get('title', '')}"
    if "save_iteration_summary" in name:
        return f"Summary: {inp.get('summary', '')[:50]}..."
    if "execute_code" in name:
        return "Code execution"
    if name == "Skill":
        skill_name = inp.get("skill", "unknown")
        return f"Skill: {skill_name}"

    # 3. Just the tool name
    return str(name.split("__")[-1] if "__" in name else name)


def parse_transcript_actions(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Parse a transcript to extract actions with their reasoning and results.

    Args:
        transcript: List of transcript entries from iterN_transcript.json

    Returns:
        List of action dicts with: tool_name, description, input, result, success
    """
    actions: list[dict[str, Any]] = []
    tool_results = _collect_tool_results_by_id(transcript)

    for item in _iter_assistant_tool_uses(transcript):
        tool_name = item.get("name", "")
        short_name = _short_tool_name(tool_name)
        if not _is_open_scientist_tool(tool_name, short_name):
            continue

        tool_use_id = item.get("id")
        if not isinstance(tool_use_id, str):
            continue
        result = tool_results.get(tool_use_id, {})
        result_text = _extract_tool_result_text(result)
        actions.append(
            {
                "tool_name": tool_name,
                "short_name": short_name,
                "description": get_action_description(item),
                "input": item.get("input", {}),
                "result": result_text,
                "success": _is_success_from_result_text(result_text),
            }
        )

    return actions


def extract_usage_summary(transcript: list[dict[str, Any]]) -> UsageSummary:
    """
    Extract a summary of tool and skill usage from a transcript.

    Args:
        transcript: List of transcript entries

    Returns:
        UsageSummary with counts and skill names
    """
    summary = UsageSummary()

    for item in _iter_assistant_tool_uses(transcript):
        tool_name = item.get("name", "")
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
        elif _is_open_scientist_tool(tool_name, short_name):
            summary.mcp_tool_calls += 1

        if tool_name == "Skill":
            skill_name = item.get("input", {}).get("skill", "")
            if skill_name:
                summary.skill_invocations.append(skill_name)

    return summary
