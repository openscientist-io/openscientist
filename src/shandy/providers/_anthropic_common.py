"""Shared helpers for Anthropic-compatible providers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def resolve_model_name(
    *,
    model_override: str | None,
    configured_model: str | None,
    provider_default_model: str,
) -> str:
    """Resolve model using consistent precedence across providers."""
    return model_override or configured_model or provider_default_model


def build_message_params(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Normalize plain role/content messages for Anthropic-compatible SDKs."""
    return [{"role": msg["role"], "content": msg["content"]} for msg in messages]


def extract_first_text_block(content_blocks: Sequence[Any]) -> str:
    """Extract text from the first content block when present."""
    if not content_blocks:
        return ""
    first_block = content_blocks[0]
    text = getattr(first_block, "text", None)
    return text if isinstance(text, str) else ""


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


def send_anthropic_message(
    *,
    client: Any,
    messages: list[dict[str, str]],
    system: str | None,
    model: str | None,
    configured_model: str | None,
    provider_default_model: str,
    max_tokens: int,
) -> str:
    """Send a plain message and return the first text block."""
    effective_model = resolve_model_name(
        model_override=model,
        configured_model=configured_model,
        provider_default_model=provider_default_model,
    )
    typed_messages = build_message_params(messages)
    response = client.messages.create(
        model=effective_model,
        max_tokens=max_tokens,
        system=system or "",
        messages=typed_messages,
    )
    return extract_first_text_block(response.content)


def send_anthropic_message_with_tools(
    *,
    client: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    system: str | None,
    model: str | None,
    configured_model: str | None,
    provider_default_model: str,
    max_tokens: int,
    tool_use_block_type: type[Any],
) -> dict[str, Any]:
    """Send a message with tools and return normalized stop/content/usage payload."""
    effective_model = resolve_model_name(
        model_override=model,
        configured_model=configured_model,
        provider_default_model=provider_default_model,
    )
    response = client.messages.create(
        model=effective_model,
        max_tokens=max_tokens,
        system=build_system_blocks(system),  # type: ignore[arg-type]
        messages=messages,  # type: ignore[arg-type]
        tools=build_tool_params(tools),
    )
    return {
        "stop_reason": response.stop_reason,
        "content": convert_response_blocks(
            response.content,
            tool_use_block_type=tool_use_block_type,
        ),
        "model": response.model,
        "usage": build_usage_dict(response.usage),
    }
