"""Shared helpers for Anthropic-compatible providers."""

from __future__ import annotations

from typing import Any


def build_system_blocks(system: str | None) -> list[dict[str, Any]]:
    """Build system prompt blocks with ephemeral cache control."""
    if not system:
        return []
    return [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def build_tool_params(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize tool definitions to Anthropic tool parameter shape."""
    return [
        {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "input_schema": tool["input_schema"],
        }
        for tool in tools
    ]


def convert_response_blocks(
    blocks: list[Any],
    *,
    tool_use_block_type: type[Any],
) -> list[dict[str, Any]]:
    """Convert Anthropic SDK content blocks to a serializable dict list."""
    converted: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, tool_use_block_type):
            converted.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
            continue
        if hasattr(block, "text"):
            converted.append({"type": "text", "text": block.text})
    return converted


def build_usage_dict(usage: Any) -> dict[str, int]:
    """Build a usage dict with optional cache token fields."""
    usage_dict = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    }
    if hasattr(usage, "cache_creation_input_tokens"):
        usage_dict["cache_creation_input_tokens"] = int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
    if hasattr(usage, "cache_read_input_tokens"):
        usage_dict["cache_read_input_tokens"] = int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
    return usage_dict
