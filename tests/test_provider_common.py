"""Tests for shared Anthropic-provider helpers."""

from types import SimpleNamespace

from openscientist.providers._anthropic_common import (
    build_system_blocks,
    build_tool_params,
    build_usage_dict,
    convert_response_blocks,
    resolve_model_name,
    send_anthropic_message,
    send_anthropic_message_with_tools,
)


def test_build_system_blocks_empty_and_nonempty():
    assert build_system_blocks(None) == []
    assert build_system_blocks("") == []
    blocks = build_system_blocks("system prompt")
    assert blocks == [
        {
            "type": "text",
            "text": "system prompt",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_build_tool_params():
    tool_params = build_tool_params(
        [
            {
                "name": "search_pubmed",
                "description": "Search papers",
                "input_schema": {"type": "object"},
            }
        ]
    )
    assert tool_params == [
        {
            "name": "search_pubmed",
            "description": "Search papers",
            "input_schema": {"type": "object"},
        }
    ]


def test_convert_response_blocks():
    class TextBlock:
        def __init__(self, text: str):
            self.text = text

    class ToolUseBlock:
        def __init__(self):
            self.id = "tool_1"
            self.name = "execute_code"
            self.input = {"code": "print(1)"}

    blocks = convert_response_blocks(
        [TextBlock("hello"), ToolUseBlock()],
        tool_use_block_type=ToolUseBlock,
    )
    assert blocks == [
        {"type": "text", "text": "hello"},
        {
            "type": "tool_use",
            "id": "tool_1",
            "name": "execute_code",
            "input": {"code": "print(1)"},
        },
    ]


def test_build_usage_dict_handles_optional_cache_fields():
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=25,
        cache_creation_input_tokens=40,
        cache_read_input_tokens=12,
    )
    assert build_usage_dict(usage) == {
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_creation_input_tokens": 40,
        "cache_read_input_tokens": 12,
    }

    usage_without_cache = SimpleNamespace(input_tokens=5, output_tokens=2)
    assert build_usage_dict(usage_without_cache) == {
        "input_tokens": 5,
        "output_tokens": 2,
    }


def test_resolve_model_name_precedence():
    assert (
        resolve_model_name(
            model_override="override-model",
            configured_model="configured-model",
            provider_default_model="provider-default",
        )
        == "override-model"
    )
    assert (
        resolve_model_name(
            model_override=None,
            configured_model="configured-model",
            provider_default_model="provider-default",
        )
        == "configured-model"
    )
    assert (
        resolve_model_name(
            model_override=None,
            configured_model=None,
            provider_default_model="provider-default",
        )
        == "provider-default"
    )


def test_send_anthropic_message_uses_resolved_model_and_extracts_text():
    calls: list[dict[str, object]] = []

    class FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(text="hello")])

    client = SimpleNamespace(messages=FakeMessages())
    result = send_anthropic_message(
        client=client,
        messages=[{"role": "user", "content": "ping"}],
        system=None,
        model=None,
        configured_model="configured-model",
        provider_default_model="provider-default",
        max_tokens=123,
    )

    assert result == "hello"
    assert calls[0]["model"] == "configured-model"
    assert calls[0]["system"] == ""
    assert calls[0]["messages"] == [{"role": "user", "content": "ping"}]


def test_send_anthropic_message_with_tools_returns_normalized_payload():
    calls: list[dict[str, object]] = []

    class FakeToolUseBlock:
        def __init__(self) -> None:
            self.id = "tool_1"
            self.name = "lookup"
            self.input = {"q": "x"}

    class FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            usage = SimpleNamespace(input_tokens=3, output_tokens=5)
            return SimpleNamespace(
                stop_reason="tool_use",
                content=[FakeToolUseBlock(), SimpleNamespace(text="done")],
                model="provider-default",
                usage=usage,
            )

    client = SimpleNamespace(messages=FakeMessages())
    result = send_anthropic_message_with_tools(
        client=client,
        messages=[{"role": "user", "content": "ping"}],
        tools=[{"name": "lookup", "description": "Lookup", "input_schema": {"type": "object"}}],
        system="system prompt",
        model=None,
        configured_model=None,
        provider_default_model="provider-default",
        max_tokens=321,
        tool_use_block_type=FakeToolUseBlock,
    )

    assert calls[0]["model"] == "provider-default"
    assert result["stop_reason"] == "tool_use"
    assert result["content"] == [
        {
            "type": "tool_use",
            "id": "tool_1",
            "name": "lookup",
            "input": {"q": "x"},
        },
        {"type": "text", "text": "done"},
    ]
    assert result["usage"] == {"input_tokens": 3, "output_tokens": 5}
