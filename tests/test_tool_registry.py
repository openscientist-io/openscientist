"""Tests for the tool registry bridge adapter."""

from __future__ import annotations

import importlib.util

import pytest


class TestBridgeHelpers:
    """Tests for _build_input_schema, _extract_description, _python_type_to_json_schema."""

    def test_python_type_to_json_schema_str(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(str) == {"type": "string"}

    def test_python_type_to_json_schema_int(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(int) == {"type": "integer"}

    def test_python_type_to_json_schema_float(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(float) == {"type": "number"}

    def test_python_type_to_json_schema_bool(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(bool) == {"type": "boolean"}

    def test_python_type_to_json_schema_list(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(list) == {"type": "array"}

    def test_python_type_to_json_schema_list_str(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(list[str]) == {
            "type": "array",
            "items": {"type": "string"},
        }

    def test_python_type_to_json_schema_dict(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(dict) == {"type": "object"}

    def test_python_type_to_json_schema_optional_str(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(str | None) == {"type": "string"}

    def test_python_type_to_json_schema_optional_dict(self) -> None:
        from open_scientist.tools.registry import _python_type_to_json_schema

        assert _python_type_to_json_schema(dict | None) == {"type": "object"}

    def test_extract_description_from_docstring(self) -> None:
        from open_scientist.tools.registry import _extract_description

        def my_func() -> str:
            """Execute Python code to analyze data.

            The code has access to 'data' and other things.

            Args:
                code: Python code to execute
            """
            return ""

        assert _extract_description(my_func) == "Execute Python code to analyze data."

    def test_extract_description_no_docstring(self) -> None:
        from open_scientist.tools.registry import _extract_description

        def bare_func() -> str:
            return ""

        assert _extract_description(bare_func) == "bare_func"

    def test_extract_description_single_line(self) -> None:
        from open_scientist.tools.registry import _extract_description

        def simple() -> str:
            """A simple tool."""
            return ""

        assert _extract_description(simple) == "A simple tool."

    def test_build_input_schema_required_params(self) -> None:
        from open_scientist.tools.registry import _build_input_schema

        def my_tool(query: str, count: int) -> str:
            """A tool."""
            _ = (query, count)
            return ""

        schema = _build_input_schema(my_tool)
        assert schema["type"] == "object"
        assert schema["properties"]["query"] == {"type": "string"}
        assert schema["properties"]["count"] == {"type": "integer"}
        assert schema["required"] == ["query", "count"]

    def test_build_input_schema_with_defaults(self) -> None:
        from open_scientist.tools.registry import _build_input_schema

        def my_tool(query: str, max_results: int = 10, desc: str = "") -> str:
            """A tool."""
            _ = (query, max_results, desc)
            return ""

        schema = _build_input_schema(my_tool)
        assert schema["required"] == ["query"]
        assert "max_results" in schema["properties"]
        assert "desc" in schema["properties"]

    def test_build_input_schema_complex_types(self) -> None:
        from open_scientist.tools.registry import _build_input_schema

        def my_tool(
            files: list[str],
            args: dict | None = None,
            flag: bool = False,
            score: float = 0.0,
        ) -> str:
            """A tool."""
            _ = (files, args, flag, score)
            return ""

        schema = _build_input_schema(my_tool)
        assert schema["required"] == ["files"]
        assert schema["properties"]["files"] == {
            "type": "array",
            "items": {"type": "string"},
        }
        assert schema["properties"]["args"] == {"type": "object"}
        assert schema["properties"]["flag"] == {"type": "boolean"}
        assert schema["properties"]["score"] == {"type": "number"}


class TestToolBridge:
    """Tests for the tool() bridge decorator itself."""

    def test_bridge_creates_sdk_tool(self) -> None:
        """Verify tool() returns an SdkMcpTool with correct name/description/schema."""
        try:
            from claude_agent_sdk import SdkMcpTool
        except ImportError:
            pytest.skip("claude_agent_sdk not installed")

        from open_scientist.tools.registry import tool

        @tool
        def greet(name: str) -> str:
            """Say hello to someone."""
            return f"Hello, {name}!"

        assert isinstance(greet, SdkMcpTool)
        assert greet.name == "greet"
        assert greet.description == "Say hello to someone."
        assert greet.input_schema["type"] == "object"
        assert greet.input_schema["properties"]["name"] == {"type": "string"}
        assert greet.input_schema["required"] == ["name"]

    async def test_bridge_handler_unpacks_args(self) -> None:
        """Verify the async handler calls the original function with **args."""
        try:
            from claude_agent_sdk import SdkMcpTool
        except ImportError:
            pytest.skip("claude_agent_sdk not installed")

        from open_scientist.tools.registry import tool

        call_log: list[dict] = []

        @tool
        def my_tool(query: str, count: int = 5) -> str:
            """Search for items."""
            call_log.append({"query": query, "count": count})
            return f"Found {count} items for {query}"

        assert isinstance(my_tool, SdkMcpTool)

        result = await my_tool.handler({"query": "test", "count": 3})
        assert call_log == [{"query": "test", "count": 3}]
        assert result == {"content": [{"type": "text", "text": "Found 3 items for test"}]}

    async def test_bridge_wraps_return_value(self) -> None:
        """Verify string return is wrapped in SDK format."""
        if not importlib.util.find_spec("claude_agent_sdk"):
            pytest.skip("claude_agent_sdk not installed")

        from open_scientist.tools.registry import tool

        @tool
        def echo(msg: str) -> str:
            """Echo a message."""
            return f"Echo: {msg}"

        result = await echo.handler({"msg": "hello"})
        assert result == {"content": [{"type": "text", "text": "Echo: hello"}]}

    async def test_bridge_handles_defaults(self) -> None:
        """Verify optional params with defaults are filled in when missing."""
        if not importlib.util.find_spec("claude_agent_sdk"):
            pytest.skip("claude_agent_sdk not installed")

        from open_scientist.tools.registry import tool

        @tool
        def search(query: str, limit: int = 10, desc: str = "") -> str:
            """Search things."""
            return f"{query} limit={limit} desc={desc!r}"

        # Call with only required param — defaults should fill in
        result = await search.handler({"query": "foo"})
        assert result == {"content": [{"type": "text", "text": "foo limit=10 desc=''"}]}

    def test_bridge_extracts_docstring(self) -> None:
        """Verify first paragraph of docstring becomes description."""
        try:
            from claude_agent_sdk import SdkMcpTool
        except ImportError:
            pytest.skip("claude_agent_sdk not installed")

        from open_scientist.tools.registry import tool

        @tool
        def complex_tool(code: str) -> str:
            """Execute Python code to analyze data.

            The code has access to:
            - 'data': DataFrame with tabular data
            - 'data_files': List of file metadata

            Args:
                code: Python code to execute

            Returns:
                Formatted result
            """
            return code

        assert isinstance(complex_tool, SdkMcpTool)
        assert complex_tool.description == "Execute Python code to analyze data."

    async def test_bridge_handles_async_function(self) -> None:
        """Verify async tool functions are awaited directly."""
        try:
            from claude_agent_sdk import SdkMcpTool
        except ImportError:
            pytest.skip("claude_agent_sdk not installed")

        from open_scientist.tools.registry import tool

        @tool
        async def async_tool(query: str) -> str:
            """An async tool."""
            return f"Async result: {query}"

        assert isinstance(async_tool, SdkMcpTool)
        result = await async_tool.handler({"query": "test"})
        assert result == {"content": [{"type": "text", "text": "Async result: test"}]}


class TestRegistryHelpers:
    """Tests for registry helper functions."""

    def test_extract_description_and_build_schema(self) -> None:
        """Helper functions correctly extract metadata from typed functions."""
        from open_scientist.tools.registry import _build_input_schema, _extract_description

        def my_func(x: str) -> str:
            """Do something."""
            return x

        assert _extract_description(my_func) == "Do something."
        schema = _build_input_schema(my_func)
        assert schema["properties"]["x"] == {"type": "string"}
