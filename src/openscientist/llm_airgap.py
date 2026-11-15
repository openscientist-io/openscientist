"""What an air-gapped run may ask of its LLM provider.

An air-gapped container reaches only the web's proxies, so the provider is the
one party that could still reach the network for it, through a tool the
provider runs (web search and fetch, remote MCP servers) or through state
stored outside the run (a stored prompt, a conversation, another run's
response). A request from an air-gapped run may therefore define only tools the
harness runs itself, and must parse the same way for the proxy and the
provider.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import NoReturn

from openscientist.providers import provider_class
from openscientist.providers.base import ClaudeCompatible, CodexCompatible, OpenAiWireCompatible

_REFUSAL = "Refused for an air-gapped run: "


class _UnreadableRequestError(ValueError):
    """A request body the proxy cannot be sure the provider reads as it does."""


def _object_without_repeats(pairs: list[tuple[str, object]]) -> dict[str, object]:
    parsed = dict(pairs)
    if len(parsed) != len(pairs):
        raise _UnreadableRequestError("a JSON object repeats a key")
    return parsed


def _no_constant(name: str) -> NoReturn:
    raise _UnreadableRequestError(f"the body holds {name}")


def _parse(payload: bytes | str) -> dict[str, object] | None:
    """The request object, or None when the body is not a JSON object whose
    every key appears once, since a provider may keep a different copy of a
    repeated key than Python does."""
    try:
        request = json.loads(
            payload, object_pairs_hook=_object_without_repeats, parse_constant=_no_constant
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return request if isinstance(request, dict) else None


def _tool_list(value: object) -> list[object] | None:
    """The tools of a ``tools`` field, or None when the field is not a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else None


def _anthropic_tool_allowed(tool: object) -> bool:
    """Whether an Anthropic Messages tool is one the harness runs, which is a
    tool without a type or of type ``custom``. Every typed tool Anthropic
    defines either runs on Anthropic's side or names a remote server."""
    return isinstance(tool, dict) and tool.get("type") in (None, "custom")


def _responses_tool_allowed(tool: object) -> bool:
    """Whether a Responses tool is one the harness runs: a function, a custom
    tool, a tool search the client executes, or a namespace of those."""
    if not isinstance(tool, dict):
        return False
    kind = tool.get("type")
    if kind in ("function", "custom"):
        return True
    if kind == "tool_search":
        return tool.get("execution") == "client"
    if kind == "namespace":
        inner = _tool_list(tool.get("tools"))
        return inner is not None and all(_responses_tool_allowed(each) for each in inner)
    return False


def _chat_tool_allowed(tool: object) -> bool:
    return isinstance(tool, dict) and tool.get("type") == "function"


def _anthropic_refusal(request: dict[str, object]) -> str | None:
    for field in ("mcp_servers", "container"):
        if field in request:
            return f"`{field}` makes the provider reach past the run"
    tools = _tool_list(request.get("tools"))
    if tools is None or not all(_anthropic_tool_allowed(tool) for tool in tools):
        return "only tools the harness runs are allowed"
    return None


def _responses_refusal(
    request: dict[str, object], previous_response_ids: Collection[str]
) -> str | None:
    for field in ("conversation", "prompt"):
        if field in request:
            return f"`{field}` brings in state stored outside the run"
    previous = request.get("previous_response_id")
    if previous is not None and previous not in previous_response_ids:
        return "`previous_response_id` names a response this run was not given"
    tools = _tool_list(request.get("tools"))
    if tools is None or not all(_responses_tool_allowed(tool) for tool in tools):
        return "only tools the harness runs are allowed"
    items = request.get("input")
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or "tools" not in item:
            continue
        loaded = _tool_list(item["tools"])
        if loaded is None or not all(_responses_tool_allowed(tool) for tool in loaded):
            return "only tools the harness runs may be loaded"
    return None


def _chat_refusal(request: dict[str, object]) -> str | None:
    if "web_search_options" in request:
        return "`web_search_options` makes the provider search the web"
    tools = _tool_list(request.get("tools"))
    if tools is None or not all(_chat_tool_allowed(tool) for tool in tools):
        return "only tools the harness runs are allowed"
    return None


def airgap_refusal(
    provider_id: str,
    payload: bytes | str,
    *,
    previous_response_ids: Collection[str] = (),
) -> str | None:
    """Why the proxy refuses ``payload`` from an air-gapped run of
    ``provider_id``, or None when it may be forwarded.

    ``previous_response_ids`` are the responses the run received on the same
    connection, the only ones a Responses request may continue from.
    """
    request = _parse(payload)
    if request is None:
        return _REFUSAL + "the body must be a JSON object that repeats no key"
    try:
        wire = provider_class(provider_id)
    except ValueError:
        return _REFUSAL + f"provider {provider_id!r} has no air-gap rule"
    if issubclass(wire, ClaudeCompatible):
        reason = _anthropic_refusal(request)
    elif issubclass(wire, CodexCompatible):
        reason = _responses_refusal(request, previous_response_ids)
    elif issubclass(wire, OpenAiWireCompatible):
        reason = _chat_refusal(request)
    else:
        reason = f"provider {provider_id!r} has no air-gap rule"
    return None if reason is None else _REFUSAL + reason


def http_airgap_refusal(
    provider_id: str, method: str, content_encoding: str, body: bytes
) -> str | None:
    """Why the proxy refuses an HTTP request from an air-gapped run, or None.

    A bodiless listing or probe carries nothing to check. An encoded body is
    refused, since the proxy would check bytes the provider decodes into
    something else. No HTTP request continues a response, because the
    harnesses chain responses only over the websocket.
    """
    if content_encoding.strip().lower() not in ("", "identity"):
        return _REFUSAL + "the body must not be content-encoded"
    if not body and method in ("GET", "HEAD"):
        return None
    return airgap_refusal(provider_id, body)
