"""Tests for shared Anthropic-provider helpers."""

from types import SimpleNamespace

from shandy.providers._anthropic_common import (
    build_system_blocks,
    build_tool_params,
    build_usage_dict,
    convert_response_blocks,
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
