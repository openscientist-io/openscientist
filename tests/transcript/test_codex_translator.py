"""Tests for the Codex :class:`TranscriptDeserializer`.

Covers happy-path translation for every supported official ``openai-codex``
item type, envelope event handling, the :class:`UnknownEntry` contract, and a
real-capture sweep over fixtures of official items.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from openscientist.transcript import (
    CODEX,
    AssistantText,
    CodexAgent,
    CodexDeserializer,
    CollabAgentToolCall,
    FileChange,
    Plan,
    Reasoning,
    ShellExecution,
    TaskNotification,
    TaskStarted,
    ToolCall,
    ToolResult,
    TranscriptAdapter,
    TranscriptDeserializer,
    UnknownEntry,
    UserPrompt,
    WebSearch,
)


def _envelope(thread_id: str = "thread-x") -> list[dict[str, Any]]:
    return [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
    ]


def _item_completed(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "item.completed", "item": item}


def _closing() -> dict[str, Any]:
    return {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}


# ---- Protocol surface --------------------------------------------------------------------------


class TestCodexDeserializerSurface:
    def test_module_singleton_is_a_codex_deserializer(self) -> None:
        assert isinstance(CODEX, CodexDeserializer)

    def test_module_singleton_satisfies_the_protocol(self) -> None:
        assert isinstance(CODEX, TranscriptDeserializer)

    def test_codex_agent_is_a_marker(self) -> None:
        from openscientist.transcript import AgentMarker

        assert issubclass(CodexAgent, AgentMarker)


# ---- Envelope events ---------------------------------------------------------------------------


class TestEnvelopeEvents:
    """Envelope events that aren't items still have their fields preserved.

    ``CodexAgent`` itself only feeds ``item.completed`` events, but the
    deserializer keeps handling a full event stream for other callers.
    """

    def test_thread_started_becomes_task_started_with_thread_metadata(self) -> None:
        out = CODEX.deserialize([{"type": "thread.started", "thread_id": "thr-001"}, _closing()])
        assert isinstance(out[0], TaskStarted)
        assert out[0].task_id == "thr-001"
        assert out[0].task_type == "thread"
        assert out[0].session_id == "thr-001"

    def test_turn_completed_becomes_task_notification_with_usage(self) -> None:
        out = CODEX.deserialize(
            [
                {"type": "thread.started", "thread_id": "thr-003"},
                {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 25}},
            ]
        )
        notif = out[-1]
        assert isinstance(notif, TaskNotification)
        assert notif.status == "completed"
        assert notif.usage == {"input_tokens": 100, "output_tokens": 25}
        assert notif.task_id == "thr-003"

    def test_turn_failed_becomes_task_notification_with_error_message(self) -> None:
        out = CODEX.deserialize(
            [
                {"type": "thread.started", "thread_id": "thr-004"},
                {"type": "turn.failed", "error": {"message": "rate limited"}},
            ]
        )
        notif = out[-1]
        assert isinstance(notif, TaskNotification)
        assert notif.status == "failed"
        assert notif.summary == "rate limited"

    def test_top_level_error_becomes_task_notification(self) -> None:
        out = CODEX.deserialize(
            [
                {"type": "thread.started", "thread_id": "thr-005"},
                {"type": "error", "message": "stream interrupted"},
            ]
        )
        notif = out[-1]
        assert isinstance(notif, TaskNotification)
        assert notif.status == "failed"
        assert notif.summary == "stream interrupted"

    def test_item_started_and_updated_are_skipped(self) -> None:
        events = _envelope("thr-006") + [
            {
                "type": "item.started",
                "item": {"id": "i1", "type": "commandExecution", "command": "ls", "status": "in"},
            },
            {
                "type": "item.updated",
                "item": {"id": "i1", "type": "commandExecution", "command": "ls", "status": "in"},
            },
            _item_completed(
                {
                    "id": "i1",
                    "type": "commandExecution",
                    "command": "ls",
                    "aggregated_output": "a\nb\n",
                    "exit_code": 0,
                    "status": "completed",
                }
            ),
            _closing(),
        ]
        out = CODEX.deserialize(events)
        shells = [e for e in out if isinstance(e, ShellExecution)]
        assert len(shells) == 1
        assert shells[0].output == "a\nb\n"
        assert shells[0].exit_code == 0


# ---- Per-item type translation -----------------------------------------------------------------


class TestAgentMessageItem:
    def test_emits_assistant_text(self) -> None:
        events = (
            _envelope()
            + [_item_completed({"id": "i1", "type": "agentMessage", "text": "hello"})]
            + [_closing()]
        )
        out = CODEX.deserialize(events)
        msgs = [e for e in out if isinstance(e, AssistantText)]
        assert msgs == [AssistantText(id="i1", text="hello", raw=msgs[0].raw)]
        assert msgs[0].raw["_thread_id"] == "thread-x"


class TestReasoningItem:
    def test_text_comes_from_summary(self) -> None:
        events = (
            _envelope()
            + [_item_completed({"id": "r1", "type": "reasoning", "summary": ["step by step"]})]
            + [_closing()]
        )
        out = CODEX.deserialize(events)
        reasons = [e for e in out if isinstance(e, Reasoning)]
        assert len(reasons) == 1
        assert reasons[0].text == "step by step"
        assert reasons[0].id == "r1"

    def test_falls_back_to_content_when_no_summary(self) -> None:
        events = _envelope() + [
            _item_completed(
                {"id": "r2", "type": "reasoning", "summary": [], "content": ["raw cot"]}
            ),
            _closing(),
        ]
        reasons = [e for e in CODEX.deserialize(events) if isinstance(e, Reasoning)]
        assert reasons[0].text == "raw cot"


class TestCommandExecutionItem:
    def test_completed_emits_shell_execution_with_aggregated_output(self) -> None:
        item = {
            "id": "c1",
            "type": "commandExecution",
            "command": "ls /tmp",
            "aggregated_output": "a\nb\n",
            "exit_code": 0,
            "status": "completed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        shells = [e for e in out if isinstance(e, ShellExecution)]
        assert len(shells) == 1
        assert shells[0].command == "ls /tmp"
        assert shells[0].output == "a\nb\n"
        assert shells[0].exit_code == 0
        assert shells[0].status == "completed"

    def test_missing_aggregated_output_becomes_empty_string(self) -> None:
        item = {
            "id": "c2",
            "type": "commandExecution",
            "command": "true",
            "exit_code": 0,
            "status": "completed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        shells = [e for e in out if isinstance(e, ShellExecution)]
        assert shells[0].output == ""


class TestFileChangeItem:
    def test_one_change_emits_one_file_change(self) -> None:
        item = {
            "id": "f1",
            "type": "fileChange",
            "changes": [{"path": "/tmp/foo", "kind": {"type": "add"}}],
            "status": "completed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        fcs = [e for e in out if isinstance(e, FileChange)]
        assert len(fcs) == 1
        assert fcs[0].path == "/tmp/foo"
        assert fcs[0].kind == "create"
        assert fcs[0].success is True

    def test_multiple_changes_split_sharing_parent_id(self) -> None:
        item = {
            "id": "f2",
            "type": "fileChange",
            "changes": [
                {"path": "/a", "kind": {"type": "add"}},
                {"path": "/b", "kind": {"type": "update"}},
                {"path": "/c", "kind": {"type": "delete"}},
            ],
            "status": "completed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        fcs = [e for e in out if isinstance(e, FileChange)]
        assert [fc.path for fc in fcs] == ["/a", "/b", "/c"]
        assert [fc.kind for fc in fcs] == ["create", "edit", "delete"]
        assert {fc.id for fc in fcs} == {"f2"}

    def test_failed_status_sets_success_false(self) -> None:
        item = {
            "id": "f3",
            "type": "fileChange",
            "changes": [{"path": "/x", "kind": {"type": "add"}}],
            "status": "failed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        fcs = [e for e in out if isinstance(e, FileChange)]
        assert fcs[0].success is False

    def test_unknown_kind_becomes_unknown_entry(self) -> None:
        item = {
            "id": "f4",
            "type": "fileChange",
            "changes": [{"path": "/x", "kind": {"type": "scramble"}}],
            "status": "completed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        unknowns = [e for e in out if isinstance(e, UnknownEntry)]
        assert len(unknowns) == 1
        assert "scramble" in str(unknowns[0].raw)


class TestMcpToolCallItem:
    def test_success_emits_paired_tool_call_and_tool_result(self) -> None:
        item = {
            "id": "m1",
            "type": "mcpToolCall",
            "server": "openscientist-tools",
            "tool": "execute_code",
            "arguments": {"code": "print(1)", "language": "python"},
            "result": {
                "content": [{"type": "text", "text": "1\n"}],
                "structured_content": {"value": 1},
            },
            "error": None,
            "status": "completed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        calls = [e for e in out if isinstance(e, ToolCall)]
        results = [e for e in out if isinstance(e, ToolResult)]
        assert len(calls) == 1 and len(results) == 1
        assert calls[0].id == results[0].call_id == "m1"
        assert calls[0].tool == "execute_code"
        assert calls[0].server == "openscientist-tools"
        assert calls[0].arguments == {"code": "print(1)", "language": "python"}
        assert results[0].success is True
        assert results[0].output == "1\n"
        assert results[0].structured_content == {"value": 1}

    def test_failure_emits_tool_result_with_error_message(self) -> None:
        item = {
            "id": "m2",
            "type": "mcpToolCall",
            "server": "openscientist-tools",
            "tool": "execute_code",
            "arguments": {},
            "result": None,
            "error": {"message": "exited with code 2"},
            "status": "failed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        results = [e for e in out if isinstance(e, ToolResult)]
        assert results[0].success is False
        assert results[0].error_message == "exited with code 2"


class TestCollabToolCallItem:
    def test_emits_collab_agent_tool_call(self) -> None:
        item = {
            "id": "ct1",
            "type": "collabAgentToolCall",
            "prompt": "investigate this",
            "agents_states": {"child-thread": {"status": "completed", "message": "done"}},
            "status": "completed",
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        collabs = [e for e in out if isinstance(e, CollabAgentToolCall)]
        assert len(collabs) == 1
        assert collabs[0].prompt == "investigate this"
        assert collabs[0].agents_states == {
            "child-thread": {"status": "completed", "message": "done"}
        }


class TestWebSearchItem:
    def test_emits_web_search(self) -> None:
        item = {
            "id": "w1",
            "type": "webSearch",
            "query": "kor torpor",
            "action": {"type": "search", "query": "kor torpor"},
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        searches = [e for e in out if isinstance(e, WebSearch)]
        assert len(searches) == 1
        assert searches[0].query == "kor torpor"
        assert searches[0].action == {"type": "search", "query": "kor torpor"}


class TestPlanItem:
    def test_emits_plan_with_text(self) -> None:
        item = {"id": "p1", "type": "plan", "text": "1. do this\n2. do that"}
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        plans = [e for e in out if isinstance(e, Plan)]
        assert len(plans) == 1
        assert plans[0].text == "1. do this\n2. do that"


class TestUserMessageItem:
    def test_emits_user_prompt_from_content(self) -> None:
        item = {
            "id": "u1",
            "type": "userMessage",
            "content": [{"type": "text", "text": "explain that again"}],
        }
        out = CODEX.deserialize(_envelope() + [_item_completed(item), _closing()])
        prompts = [e for e in out if isinstance(e, UserPrompt)]
        assert prompts == [UserPrompt(id="u1", text="explain that again", raw=prompts[0].raw)]


# ---- Unknown handling -------------------------------------------------------------------------


class TestUnknownHandling:
    def test_unknown_envelope_type_becomes_unknown_entry(self) -> None:
        out = CODEX.deserialize(
            [
                {"type": "thread.started", "thread_id": "thr-u"},
                {"type": "thread.surprising", "payload": "?"},
                _closing(),
            ]
        )
        unknowns = [e for e in out if isinstance(e, UnknownEntry)]
        assert len(unknowns) == 1
        assert unknowns[0].source == "codex"
        assert unknowns[0].raw["type"] == "thread.surprising"

    def test_unknown_item_type_becomes_unknown_entry(self) -> None:
        events = (
            _envelope()
            + [_item_completed({"id": "x1", "type": "novelItem", "extra": 1})]
            + [_closing()]
        )
        out = CODEX.deserialize(events)
        unknowns = [e for e in out if isinstance(e, UnknownEntry)]
        assert len(unknowns) == 1
        assert unknowns[0].raw["_item"]["type"] == "novelItem"

    def test_item_completed_without_dict_item_becomes_unknown_entry(self) -> None:
        events = _envelope() + [{"type": "item.completed", "item": "not-a-dict"}, _closing()]
        out = CODEX.deserialize(events)
        unknowns = [e for e in out if isinstance(e, UnknownEntry)]
        assert len(unknowns) == 1

    def test_non_dict_event_becomes_unknown_entry(self) -> None:
        out = CODEX.deserialize([{"type": "thread.started", "thread_id": "x"}, "not-a-dict"])  # type: ignore[list-item]
        unknowns = [e for e in out if isinstance(e, UnknownEntry)]
        assert len(unknowns) == 1

    def test_unknown_emits_warning_via_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="openscientist.transcript"):
            CODEX.deserialize([{"type": "future.event_kind"}])
        assert any("UnknownEntry" in rec.message for rec in caplog.records)


# ---- Real-capture sweep ------------------------------------------------------------------------
#
# Fixtures are real captures of official-SDK ``TurnResult.items`` (one JSON file
# per turn, a list of ``item.model_dump`` dicts). ``CodexAgent`` wraps each item
# in an ``item.completed`` envelope, which is what we replay here.

ITEM_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "transcripts" / "codex" / "items"
ITEM_FIXTURES = sorted(ITEM_FIXTURE_DIR.glob("*.json"))


def _load_item_fixture(path: Path) -> list[dict[str, Any]]:
    items = json.loads(path.read_text())
    return [{"type": "item.completed", "item": item} for item in items]


class TestRealItemFixtures:
    def test_fixtures_exist(self) -> None:
        assert ITEM_FIXTURES, "no official-SDK item fixtures captured"

    @pytest.mark.parametrize("path", ITEM_FIXTURES, ids=lambda p: p.stem)
    def test_translates_without_unknown(self, path: Path) -> None:
        entries = CODEX.deserialize(_load_item_fixture(path))
        unknowns = [e for e in entries if isinstance(e, UnknownEntry)]
        assert not unknowns, f"{path.name}: produced UnknownEntry. Sample: {unknowns[0].raw}"

    @pytest.mark.parametrize("path", ITEM_FIXTURES, ids=lambda p: p.stem)
    def test_roundtrips_through_typeadapter(self, path: Path) -> None:
        entries = CODEX.deserialize(_load_item_fixture(path))
        raw = TranscriptAdapter.dump_python(entries, mode="json")
        assert TranscriptAdapter.validate_python(raw) == entries

    def test_shell_turn_produces_expected_entry_types(self) -> None:
        path = ITEM_FIXTURE_DIR / "shell_turn.json"
        entries = CODEX.deserialize(_load_item_fixture(path))
        types = [type(e).__name__ for e in entries]
        assert "ShellExecution" in types
        assert "AssistantText" in types
