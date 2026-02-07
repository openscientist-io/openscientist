"""Transcript parsing utilities for the web application."""

import json
from typing import Any, Dict, List


def get_action_description(tool_use: Dict[str, Any]) -> str:
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

    # 3. Just the tool name
    return str(name.split("__")[-1] if "__" in name else name)


def parse_transcript_actions(transcript: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Parse a transcript to extract actions with their reasoning and results.

    Args:
        transcript: List of transcript entries from iterN_transcript.json

    Returns:
        List of action dicts with: tool_name, description, input, result, success
    """
    actions = []
    tool_results = {}

    # First pass: collect all tool results by tool_use_id
    for entry in transcript:
        if entry.get("type") == "user":
            content = entry.get("message", {}).get("content", [])
            for item in content:
                if item.get("type") == "tool_result":
                    tool_use_id = item.get("tool_use_id")
                    result_content = item.get("content", "")
                    # Parse JSON string if it looks like one
                    if isinstance(result_content, str) and result_content.startswith("{"):
                        try:
                            result_content = json.loads(result_content)
                        except json.JSONDecodeError:
                            pass
                    tool_results[tool_use_id] = result_content

    # Second pass: collect tool uses and match with results
    for entry in transcript:
        if entry.get("type") == "assistant":
            content = entry.get("message", {}).get("content", [])
            for item in content:
                if item.get("type") == "tool_use":
                    tool_use_id = item.get("id")
                    tool_name = item.get("name", "")
                    inp = item.get("input", {})

                    # Skip non-shandy tools (like Read, Bash, etc.)
                    if "shandy" not in tool_name.lower():
                        continue

                    # Get the result
                    result = tool_results.get(tool_use_id, {})
                    result_text = ""
                    success = True

                    if isinstance(result, dict):
                        result_text = result.get("result", str(result))
                    elif isinstance(result, str):
                        result_text = result
                    elif isinstance(result, list):
                        # Handle list of content items
                        result_text = str(result)

                    # Determine success from result text
                    if "failed" in result_text.lower() or "error" in result_text.lower():
                        success = False

                    actions.append(
                        {
                            "tool_name": tool_name,
                            "short_name": (
                                tool_name.split("__")[-1] if "__" in tool_name else tool_name
                            ),
                            "description": get_action_description(item),
                            "input": inp,
                            "result": result_text,
                            "success": success,
                        }
                    )

    return actions
