"""Tests for transcript parsing utilities."""

from shandy.webapp_components.utils.transcript_parser import (
    get_action_description,
    parse_transcript_actions,
)


class TestGetActionDescription:
    """Tests for get_action_description function."""

    def test_explicit_description(self):
        """Test that explicit description is used when provided."""
        tool_use = {
            "name": "shandy__load_data",
            "input": {"description": "Loading test dataset"},
        }
        assert get_action_description(tool_use) == "Loading test dataset"

    def test_search_pubmed_fallback(self):
        """Test fallback for search_pubmed tool."""
        tool_use = {
            "name": "shandy__search_pubmed",
            "input": {"query": "cancer treatment"},
        }
        assert get_action_description(tool_use) == "Search: cancer treatment"

    def test_update_knowledge_state_fallback(self):
        """Test fallback for update_knowledge_state tool."""
        tool_use = {
            "name": "shandy__update_knowledge_state",
            "input": {"title": "Important finding"},
        }
        assert get_action_description(tool_use) == "Finding: Important finding"

    def test_save_iteration_summary_fallback(self):
        """Test fallback for save_iteration_summary tool with truncation."""
        tool_use = {
            "name": "shandy__save_iteration_summary",
            "input": {
                "summary": "This is a very long summary that should be truncated to fifty characters"
            },
        }
        result = get_action_description(tool_use)
        assert result.startswith("Summary: ")
        assert result.endswith("...")
        # Should be truncated to 50 chars + "Summary: " + "..."
        assert "This is a very long summary" in result

    def test_execute_code_fallback(self):
        """Test fallback for execute_code tool."""
        tool_use = {
            "name": "shandy__execute_code",
            "input": {"code": "print('hello')"},
        }
        assert get_action_description(tool_use) == "Code execution"

    def test_tool_name_with_double_underscore(self):
        """Test tool name extraction when no fallback matches."""
        tool_use = {
            "name": "shandy__custom_tool",
            "input": {},
        }
        assert get_action_description(tool_use) == "custom_tool"

    def test_tool_name_without_double_underscore(self):
        """Test tool name when no double underscore present."""
        tool_use = {
            "name": "simple_tool",
            "input": {},
        }
        assert get_action_description(tool_use) == "simple_tool"

    def test_empty_input(self):
        """Test handling of empty input."""
        tool_use = {
            "name": "shandy__some_tool",
            "input": {},
        }
        assert get_action_description(tool_use) == "some_tool"


class TestParseTranscriptActions:
    """Tests for parse_transcript_actions function."""

    def test_empty_transcript(self):
        """Test parsing empty transcript."""
        actions = parse_transcript_actions([])
        assert actions == []

    def test_single_successful_action(self):
        """Test parsing a single successful action."""
        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_123",
                            "name": "shandy__load_data",
                            "input": {
                                "description": "Loading data",
                                "path": "data.csv",
                            },
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_123",
                            "content": "Successfully loaded 100 rows",
                        }
                    ]
                },
            },
        ]

        actions = parse_transcript_actions(transcript)

        assert len(actions) == 1
        assert actions[0]["tool_name"] == "shandy__load_data"
        assert actions[0]["short_name"] == "load_data"
        assert actions[0]["description"] == "Loading data"
        assert actions[0]["input"]["path"] == "data.csv"
        assert actions[0]["result"] == "Successfully loaded 100 rows"
        assert actions[0]["success"] is True

    def test_failed_action_with_error(self):
        """Test parsing a failed action with error in result."""
        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_456",
                            "name": "shandy__analyze",
                            "input": {"method": "correlation"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_456",
                            "content": "Analysis failed: Invalid method",
                        }
                    ]
                },
            },
        ]

        actions = parse_transcript_actions(transcript)

        assert len(actions) == 1
        assert actions[0]["tool_name"] == "shandy__analyze"
        assert actions[0]["success"] is False
        assert "failed" in actions[0]["result"].lower()

    def test_json_result_parsing(self):
        """Test parsing JSON string results."""
        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_789",
                            "name": "shandy__load_data",
                            "input": {"path": "data.csv"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_789",
                            "content": '{"result": "Loaded 50 rows", "rows": 50}',
                        }
                    ]
                },
            },
        ]

        actions = parse_transcript_actions(transcript)

        assert len(actions) == 1
        assert actions[0]["result"] == "Loaded 50 rows"

    def test_invalid_json_result_parsing(self):
        """Test parsing invalid JSON string results."""
        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_invalid",
                            "name": "shandy__test",
                            "input": {},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_invalid",
                            "content": '{"invalid json without closing brace',
                        }
                    ]
                },
            },
        ]

        actions = parse_transcript_actions(transcript)

        assert len(actions) == 1
        # Should use the raw string when JSON parsing fails
        assert actions[0]["result"] == '{"invalid json without closing brace'

    def test_list_result_parsing(self):
        """Test parsing list results."""
        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_list",
                            "name": "shandy__test",
                            "input": {},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_list",
                            "content": ["item1", "item2", "item3"],
                        }
                    ]
                },
            },
        ]

        actions = parse_transcript_actions(transcript)

        assert len(actions) == 1
        # Should convert list to string
        assert "item1" in actions[0]["result"]
        assert "item2" in actions[0]["result"]
        assert "item3" in actions[0]["result"]

    def test_non_shandy_tools_filtered(self):
        """Test that non-shandy tools are filtered out."""
        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_read",
                            "name": "Read",
                            "input": {"file": "test.txt"},
                        },
                        {
                            "type": "tool_use",
                            "id": "tool_shandy",
                            "name": "shandy__analyze",
                            "input": {"method": "test"},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_read",
                            "content": "file content",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_shandy",
                            "content": "analysis result",
                        },
                    ]
                },
            },
        ]

        actions = parse_transcript_actions(transcript)

        # Only shandy tool should be included
        assert len(actions) == 1
        assert actions[0]["tool_name"] == "shandy__analyze"

    def test_multiple_actions_with_mixed_results(self):
        """Test parsing multiple actions with mixed success/failure."""
        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_1",
                            "name": "shandy__load_data",
                            "input": {"path": "data1.csv"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_1",
                            "content": "Success",
                        }
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_2",
                            "name": "shandy__analyze",
                            "input": {"method": "test"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool_2",
                            "content": "Error: Failed to analyze",
                        }
                    ]
                },
            },
        ]

        actions = parse_transcript_actions(transcript)

        assert len(actions) == 2
        assert actions[0]["success"] is True
        assert actions[1]["success"] is False

    def test_missing_tool_result(self):
        """Test handling of tool use without corresponding result."""
        transcript = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool_missing",
                            "name": "shandy__test",
                            "input": {},
                        }
                    ]
                },
            }
        ]

        actions = parse_transcript_actions(transcript)

        assert len(actions) == 1
        # When there's no result, it defaults to empty dict stringified
        assert actions[0]["result"] in ("", "{}")
        assert actions[0]["success"] is True
