"""Exhaustive tests for the Claude :class:`TranscriptDeserializer`.

Covers happy-path translation for every supported content block type,
skip-on-malformed behavior for every shape check, order preservation,
cross-entry call/result pairing, real captured ``claude-agent-sdk``
transcripts, and a sweep across any local ``jobs/`` directory.
"""

import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest

from openscientist.transcript import (
    CLAUDE,
    AssistantText,
    ClaudeAgent,
    ClaudeDeserializer,
    Reasoning,
    SessionInit,
    TaskNotification,
    TaskProgress,
    TaskStarted,
    ToolCall,
    ToolResult,
    TranscriptAdapter,
    TranscriptDeserializer,
    UnknownEntry,
    UserPrompt,
)


def _assistant(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"content": blocks}}


def _user(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "user", "message": {"content": blocks}}


# ---- Protocol surface --------------------------------------------------------------------------


class TestClaudeDeserializerSurface:
    """Pins the public surface: :data:`CLAUDE` + the Protocol it satisfies."""

    def test_module_singleton_is_a_claude_deserializer(self) -> None:
        assert isinstance(CLAUDE, ClaudeDeserializer)

    def test_module_singleton_satisfies_the_protocol(self) -> None:
        # Runtime-checkable Protocol: structural conformance is verified
        # without static-only knowledge.
        assert isinstance(CLAUDE, TranscriptDeserializer)

    def test_claude_agent_is_a_marker(self) -> None:
        from openscientist.transcript import AgentMarker

        assert issubclass(ClaudeAgent, AgentMarker)


# ---- AssistantText ------------------------------------------------------------------------------


class TestAssistantTextTranslation:
    def test_text_block_becomes_assistant_text(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "text", "text": "hello"}])])
        assert out == [AssistantText(text="hello")]

    def test_multiple_text_blocks_each_emitted_in_order(self) -> None:
        out = CLAUDE.deserialize(
            [_assistant([{"type": "text", "text": "first"}, {"type": "text", "text": "second"}])]
        )
        assert out == [AssistantText(text="first"), AssistantText(text="second")]

    def test_empty_text_string_is_kept(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "text", "text": ""}])])
        assert out == [AssistantText(text="")]


# ---- ToolCall -----------------------------------------------------------------------------------


class TestToolCallTranslation:
    def test_tool_use_block_becomes_tool_call_with_fields_mapped(self) -> None:
        block = {
            "type": "tool_use",
            "id": "toolu_abc123",
            "name": "execute_code",
            "input": {"code": "print(1)", "language": "python"},
        }
        out = CLAUDE.deserialize([_assistant([block])])
        assert out == [
            ToolCall(
                id="toolu_abc123",
                tool="execute_code",
                arguments={"code": "print(1)", "language": "python"},
            )
        ]

    def test_tool_use_missing_input_defaults_to_empty_dict(self) -> None:
        out = CLAUDE.deserialize(
            [_assistant([{"type": "tool_use", "id": "t1", "name": "set_status"}])]
        )
        assert out == [ToolCall(id="t1", tool="set_status", arguments={})]

    def test_tool_use_non_dict_input_becomes_unknown(self) -> None:
        """A non-dict ``input`` cannot be silently coerced to ``{}`` under
        the no-drop contract. The original value is preserved in
        ``UnknownEntry`` so downstream consumers can recover it.
        """
        out = CLAUDE.deserialize(
            [_assistant([{"type": "tool_use", "id": "t1", "name": "n", "input": "not-a-dict"}])]
        )
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)
        assert out[0].raw["_block"]["input"] == "not-a-dict"

    def test_tool_use_missing_id_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "tool_use", "name": "n", "input": {}}])])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)
        assert out[0].raw["_block"] == {"type": "tool_use", "name": "n", "input": {}}

    def test_tool_use_missing_name_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "tool_use", "id": "t1", "input": {}}])])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)
        assert out[0].raw["_block"] == {"type": "tool_use", "id": "t1", "input": {}}

    def test_tool_use_non_string_id_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize(
            [_assistant([{"type": "tool_use", "id": 42, "name": "n", "input": {}}])]
        )
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)


# ---- ToolResult ---------------------------------------------------------------------------------


class TestToolResultTranslation:
    def test_user_tool_result_becomes_tool_result_linked(self) -> None:
        out = CLAUDE.deserialize(
            [
                _user(
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": "result text",
                            "is_error": False,
                        }
                    ]
                )
            ]
        )
        assert out == [ToolResult(call_id="toolu_abc", output="result text", success=True)]

    def test_is_error_true_sets_success_false(self) -> None:
        out = CLAUDE.deserialize(
            [_user([{"type": "tool_result", "tool_use_id": "t", "content": "", "is_error": True}])]
        )
        assert out[0].success is False  # type: ignore[union-attr]

    def test_is_error_omitted_defaults_to_success_true(self) -> None:
        out = CLAUDE.deserialize(
            [_user([{"type": "tool_result", "tool_use_id": "t", "content": "ok"}])]
        )
        assert out[0].success is True  # type: ignore[union-attr]

    def test_content_string_passes_through(self) -> None:
        out = CLAUDE.deserialize(
            [_user([{"type": "tool_result", "tool_use_id": "t", "content": "literal"}])]
        )
        assert out[0].output == "literal"  # type: ignore[union-attr]

    def test_content_list_of_text_blocks_is_joined(self) -> None:
        out = CLAUDE.deserialize(
            [
                _user(
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": [
                                {"type": "text", "text": "line one"},
                                {"type": "text", "text": "line two"},
                            ],
                        }
                    ]
                )
            ]
        )
        assert out[0].output == "line one\nline two"  # type: ignore[union-attr]

    def test_content_list_skips_non_text_items(self) -> None:
        out = CLAUDE.deserialize(
            [
                _user(
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t",
                            "content": [
                                {"type": "image", "source": {}},
                                {"type": "text", "text": "keep me"},
                            ],
                        }
                    ]
                )
            ]
        )
        assert out[0].output == "keep me"  # type: ignore[union-attr]

    def test_content_none_becomes_empty_string(self) -> None:
        out = CLAUDE.deserialize(
            [_user([{"type": "tool_result", "tool_use_id": "t", "content": None}])]
        )
        assert out[0].output == ""  # type: ignore[union-attr]

    def test_content_dict_is_stringified(self) -> None:
        out = CLAUDE.deserialize(
            [_user([{"type": "tool_result", "tool_use_id": "t", "content": {"x": 1}}])]
        )
        assert "x" in out[0].output  # type: ignore[union-attr]

    def test_tool_result_missing_tool_use_id_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([_user([{"type": "tool_result", "content": "orphan"}])])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)

    def test_tool_result_non_string_tool_use_id_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize(
            [_user([{"type": "tool_result", "tool_use_id": 42, "content": "orphan"}])]
        )
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)


# ---- Reasoning ----------------------------------------------------------------------------------


class TestReasoningTranslation:
    def test_thinking_block_with_thinking_field_becomes_reasoning(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "thinking", "thinking": "let me think"}])])
        assert out == [Reasoning(text="let me think")]

    def test_thinking_block_falls_back_to_text_field(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "thinking", "text": "fallback path"}])])
        assert out == [Reasoning(text="fallback path")]

    def test_thinking_block_summary_optional(self) -> None:
        out = CLAUDE.deserialize(
            [_assistant([{"type": "thinking", "thinking": "long", "summary": "short summary"}])]
        )
        assert out == [Reasoning(text="long", summary="short summary")]

    def test_thinking_block_missing_text_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "thinking"}])])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)


# ---- Malformed input -----------------------------------------------------------------------------


class TestMalformedInputHandling:
    """Under the no-drop contract, malformed source shapes become
    ``UnknownEntry`` rather than being silently dropped, so downstream
    consumers see exactly what the SDK emitted."""

    def test_empty_messages_list_returns_empty(self) -> None:
        assert CLAUDE.deserialize([]) == []

    def test_non_dict_entry_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize(["not a dict"])  # type: ignore[list-item]
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)
        assert out[0].source == "claude"
        assert "_non_dict_entry" in out[0].raw

    def test_entry_without_message_key_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([{"type": "assistant"}])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)

    def test_entry_with_non_dict_message_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([{"type": "assistant", "message": "string-message"}])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)

    def test_entry_with_non_list_content_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([{"type": "assistant", "message": {"content": 42}}])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)

    def test_system_entry_with_unknown_subtype_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([{"type": "system", "subtype": "future_phase", "data": {}}])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)
        assert out[0].raw == {
            "type": "system",
            "subtype": "future_phase",
            "data": {},
        }

    def test_entry_with_unknown_type_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([{"type": "future_kind", "payload": {"x": 1}}])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)
        assert out[0].raw == {"type": "future_kind", "payload": {"x": 1}}

    def test_non_dict_content_block_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize(
            [_assistant(["not a block dict"])]  # type: ignore[list-item]
        )
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)

    def test_unknown_assistant_block_type_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "future_block", "data": "x"}])])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)
        assert out[0].raw["_block"] == {"type": "future_block", "data": "x"}

    def test_assistant_text_with_non_string_text_becomes_unknown(self) -> None:
        out = CLAUDE.deserialize([_assistant([{"type": "text", "text": 42}])])
        assert len(out) == 1
        assert isinstance(out[0], UnknownEntry)

    def test_user_text_block_becomes_user_prompt(self) -> None:
        """A text block inside a user message is the operator's free-form prompt."""
        out = CLAUDE.deserialize([_user([{"type": "text", "text": "operator says go"}])])
        assert len(out) == 1
        assert isinstance(out[0], UserPrompt)
        assert out[0].text == "operator says go"

    def test_mixed_valid_and_invalid_yields_typed_plus_unknown(self) -> None:
        """Invalid blocks become UnknownEntry interleaved with valid entries."""
        out = CLAUDE.deserialize(
            [
                _assistant(
                    [
                        {"type": "text", "text": "keep"},
                        {"type": "tool_use", "id": "t1", "name": "n", "input": {}},
                        "not a block",  # type: ignore[list-item]
                        {"type": "weird"},
                    ]
                )
            ]
        )
        kinds = [type(e).__name__ for e in out]
        assert kinds == ["AssistantText", "ToolCall", "UnknownEntry", "UnknownEntry"]


# ---- Order + pairing -----------------------------------------------------------------------------


class TestOrderAndPairing:
    def test_order_preserved_across_entries_and_blocks(self) -> None:
        out = CLAUDE.deserialize(
            [
                _assistant(
                    [
                        {"type": "text", "text": "step 1"},
                        {"type": "tool_use", "id": "t1", "name": "n", "input": {}},
                    ]
                ),
                _user([{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]),
                _assistant([{"type": "text", "text": "step 2"}]),
            ]
        )
        assert [type(e).__name__ for e in out] == [
            "AssistantText",
            "ToolCall",
            "ToolResult",
            "AssistantText",
        ]

    def test_tool_result_pairs_to_preceding_tool_call_by_id(self) -> None:
        out = CLAUDE.deserialize(
            [
                _assistant(
                    [
                        {"type": "tool_use", "id": "a", "name": "n", "input": {}},
                        {"type": "tool_use", "id": "b", "name": "n", "input": {}},
                    ]
                ),
                _user(
                    [
                        {"type": "tool_result", "tool_use_id": "b", "content": "B"},
                        {"type": "tool_result", "tool_use_id": "a", "content": "A"},
                    ]
                ),
            ]
        )
        calls = [e for e in out if isinstance(e, ToolCall)]
        results = [e for e in out if isinstance(e, ToolResult)]
        assert [c.id for c in calls] == ["a", "b"]
        assert [(r.call_id, r.output) for r in results] == [("b", "B"), ("a", "A")]
        # Every result resolves to a call we have already produced.
        call_ids = {c.id for c in calls}
        assert all(r.call_id in call_ids for r in results)

    def test_orphan_tool_result_still_emitted(self) -> None:
        """A tool_result without a matching tool_use in the same transcript
        still gets translated. Downstream consumers can decide what
        to do.
        """
        out = CLAUDE.deserialize(
            [_user([{"type": "tool_result", "tool_use_id": "unknown", "content": "x"}])]
        )
        assert out == [ToolResult(call_id="unknown", output="x", success=True)]


# ---- TypeAdapter roundtrip ----------------------------------------------------------------------


class TestTypeAdapterRoundtrip:
    def test_translator_output_is_typeadapter_serializable(self) -> None:
        out = CLAUDE.deserialize(
            [
                _assistant(
                    [
                        {"type": "text", "text": "go"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "execute_code",
                            "input": {"code": "1+1"},
                        },
                    ]
                ),
                _user(
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "2",
                            "is_error": False,
                        }
                    ]
                ),
            ]
        )
        raw = TranscriptAdapter.dump_python(out, mode="json")
        roundtripped = TranscriptAdapter.validate_python(raw)
        assert roundtripped == out


# ---- Real claude-agent-sdk fixtures --------------------------------------------------------------


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "transcripts" / "claude_agent_sdk"
ALL_FIXTURES = sorted(FIXTURE_DIR.glob("*.json.gz"))
REAL_FIXTURES = [p for p in ALL_FIXTURES if not p.stem.startswith("synthetic_")]
SYNTHETIC_FIXTURE = FIXTURE_DIR / "synthetic_all_variants.json.gz"
#: Names a local jobs directory whose transcripts the opt-in sweep translates.
LIVE_JOBS_DIR_ENV = "OPENSCIENTIST_TEST_LIVE_JOBS_DIR"


def _live_jobs_dir() -> Path | None:
    """The jobs directory the sweep reads, or None when none is named or it
    cannot be read, so an unreadable path skips the sweep instead of failing
    the collection of this file."""
    value = os.environ.get(LIVE_JOBS_DIR_ENV)
    if not value:
        return None
    path = Path(value)
    try:
        return path if path.is_dir() else None
    except OSError:
        return None


LIVE_JOBS_DIR = _live_jobs_dir()


def _load_fixture(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        loaded: list[dict[str, Any]] = json.load(f)
    return loaded


def _count_input_blocks(messages: list[dict[str, Any]]) -> dict[str, int]:
    """Count blocks per (entry_type, block_type) in the input messages."""
    counts: dict[str, int] = {
        "assistant_text": 0,
        "assistant_tool_use": 0,
        "assistant_thinking": 0,
        "user_tool_result": 0,
    }
    for entry in messages:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        entry_type = entry.get("type")
        for item in content:
            if not isinstance(item, dict):
                continue
            block_type = item.get("type")
            key = f"{entry_type}_{block_type}"
            if key in counts:
                counts[key] += 1
    return counts


def _input_identities(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Reduce each input block to a (kind, identifier) tuple in source order."""
    ids: list[tuple[str, str]] = []
    for entry in messages:
        entry_type = entry.get("type")
        for item in entry.get("message", {}).get("content", []):
            if not isinstance(item, dict):
                continue
            btype = item.get("type")
            if entry_type == "assistant" and btype == "text":
                ids.append(("text", str(item.get("text", ""))[:40]))
            elif entry_type == "assistant" and btype == "tool_use":
                ids.append(("call", str(item.get("id", ""))))
            elif entry_type == "assistant" and btype == "thinking":
                txt = item.get("thinking") or item.get("text") or ""
                ids.append(("thinking", str(txt)[:40]))
            elif entry_type == "user" and btype == "tool_result":
                ids.append(("result", str(item.get("tool_use_id", ""))))
    return ids


def _output_identities(entries: list[Any]) -> list[tuple[str, str]]:
    """Reduce each output entry to a (kind, identifier) tuple in order."""
    ids: list[tuple[str, str]] = []
    for e in entries:
        if isinstance(e, AssistantText):
            ids.append(("text", e.text[:40]))
        elif isinstance(e, ToolCall):
            ids.append(("call", e.id))
        elif isinstance(e, ToolResult):
            ids.append(("result", e.call_id))
        elif isinstance(e, Reasoning):
            ids.append(("thinking", e.text[:40]))
    return ids


class TestAllFixtures:
    """Parametrized sweep across every fixture under
    ``tests/fixtures/transcripts/claude_agent_sdk/``.
    """

    @pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
    def test_fixture_translates_without_errors(self, path: Path) -> None:
        messages = _load_fixture(path)
        entries = CLAUDE.deserialize(messages)
        assert entries, f"{path.name} produced no entries"

    @pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
    def test_fixture_yields_only_known_variant_classes(self, path: Path) -> None:
        entries = CLAUDE.deserialize(_load_fixture(path))
        allowed = (
            AssistantText,
            ToolCall,
            ToolResult,
            Reasoning,
            UserPrompt,
            SessionInit,
            TaskStarted,
            TaskProgress,
            TaskNotification,
        )
        for entry in entries:
            assert isinstance(entry, allowed), (
                f"Unexpected variant {type(entry).__name__} in {path.name}"
            )

    @pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
    def test_fixture_count_preserves_every_recognized_block(self, path: Path) -> None:
        """Every input block of a recognized type maps to exactly one entry."""
        messages = _load_fixture(path)
        in_counts = _count_input_blocks(messages)
        entries = CLAUDE.deserialize(messages)
        assert (
            sum(1 for e in entries if isinstance(e, AssistantText)) == in_counts["assistant_text"]
        )
        assert sum(1 for e in entries if isinstance(e, ToolCall)) == in_counts["assistant_tool_use"]
        assert sum(1 for e in entries if isinstance(e, ToolResult)) == in_counts["user_tool_result"]
        assert (
            sum(1 for e in entries if isinstance(e, Reasoning)) == in_counts["assistant_thinking"]
        )

    @pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
    def test_fixture_roundtrips_through_typeadapter(self, path: Path) -> None:
        entries = CLAUDE.deserialize(_load_fixture(path))
        raw = TranscriptAdapter.dump_python(entries, mode="json")
        assert TranscriptAdapter.validate_python(raw) == entries

    @pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.stem)
    def test_fixture_preserves_source_block_order(self, path: Path) -> None:
        """Output identities (kind + id-or-snippet) must match input order."""
        messages = _load_fixture(path)
        entries = CLAUDE.deserialize(messages)
        assert _output_identities(entries) == _input_identities(messages)


class TestSpecificFixtureContent:
    """Pinned content checks on individual fixtures to catch silent drift."""

    def test_smallest_capital_of_france_iteration(self) -> None:
        """``capital_of_france_iter04`` has 3 entries: 2 tool_use + 1 text
        about Paris being the final answer."""
        entries = CLAUDE.deserialize(
            _load_fixture(FIXTURE_DIR / "capital_of_france_iter04.json.gz")
        )
        assert [type(e).__name__ for e in entries] == [
            "ToolCall",
            "ToolCall",
            "AssistantText",
        ]
        assert isinstance(entries[0], ToolCall)
        assert entries[0].tool == "mcp__shandy-tools__set_status"
        assert isinstance(entries[1], ToolCall)
        assert entries[1].tool == "mcp__shandy-tools__save_iteration_summary"
        assert isinstance(entries[2], AssistantText)
        assert "Paris" in entries[2].text

    def test_capital_of_france_iter01_has_diverse_tool_kit(self) -> None:
        """The first iteration of the SPARQL run uses six distinct tools."""
        entries = CLAUDE.deserialize(
            _load_fixture(FIXTURE_DIR / "capital_of_france_iter01.json.gz")
        )
        tool_names = {e.tool for e in entries if isinstance(e, ToolCall)}
        assert len(tool_names) >= 6
        assert any("execute_code" in name for name in tool_names)
        assert any("set_consensus_answer" in name for name in tool_names)

    def test_rust_fibonacci_iter04_uses_search_pubmed(self) -> None:
        """The Rust benchmark iter04 is the only real fixture exercising
        ``search_pubmed``."""
        entries = CLAUDE.deserialize(_load_fixture(FIXTURE_DIR / "rust_fibonacci_iter04.json.gz"))
        tool_names = {e.tool for e in entries if isinstance(e, ToolCall)}
        assert any("search_pubmed" in name for name in tool_names)


class TestSyntheticFixture:
    """Per-variant assertions against ``synthetic_all_variants``."""

    def test_synthetic_yields_every_recognised_variant_class(self) -> None:
        entries = CLAUDE.deserialize(_load_fixture(SYNTHETIC_FIXTURE))
        kinds = {type(e).__name__ for e in entries}
        assert kinds == {
            "Reasoning",
            "AssistantText",
            "ToolCall",
            "ToolResult",
            "UserPrompt",
            "TaskStarted",
            "TaskProgress",
            "TaskNotification",
        }

    def test_synthetic_success_and_failure_results(self) -> None:
        entries = CLAUDE.deserialize(_load_fixture(SYNTHETIC_FIXTURE))
        results = [e for e in entries if isinstance(e, ToolResult)]
        # Two successful tool_results (one with string content, one with
        # tool_use_result metadata) plus one failure (list-of-text content).
        assert len(results) == 3
        success_results = [r for r in results if r.success]
        failure_results = [r for r in results if not r.success]
        assert len(success_results) == 2
        assert len(failure_results) == 1
        # The failure result was provided as a list of text blocks.
        # Verify the translator flattened it correctly.
        assert "SystemExit" in failure_results[0].output

    def test_synthetic_call_result_pairing(self) -> None:
        entries = CLAUDE.deserialize(_load_fixture(SYNTHETIC_FIXTURE))
        call_ids = {e.id for e in entries if isinstance(e, ToolCall)}
        result_call_ids = {e.call_id for e in entries if isinstance(e, ToolResult)}
        # Every ToolCall id appears as a ToolResult.call_id. (The reverse is
        # not required: the rich-metadata user message points back at an
        # earlier ToolCall by id, which is allowed.)
        assert call_ids <= result_call_ids

    def test_synthetic_reasoning_preserves_summary_and_signature(self) -> None:
        entries = CLAUDE.deserialize(_load_fixture(SYNTHETIC_FIXTURE))
        reasonings = [e for e in entries if isinstance(e, Reasoning)]
        assert len(reasonings) == 2
        plain = next(r for r in reasonings if r.summary == "plan")
        signed = next(r for r in reasonings if r.signature)
        assert plain.signature is None
        assert signed.summary == ["initial analysis", "follow-up consideration"]
        assert signed.signature is not None
        assert signed.signature.startswith("EvgBCsAG")

    def test_synthetic_user_prompts_recovered(self) -> None:
        entries = CLAUDE.deserialize(_load_fixture(SYNTHETIC_FIXTURE))
        prompts = [e for e in entries if isinstance(e, UserPrompt)]
        assert len(prompts) == 2
        assert prompts[0].text == "What is the capital of France?"
        assert prompts[1].text == "Follow-up: and the population?"

    def test_synthetic_assistant_wrapper_fields_lifted(self) -> None:
        entries = CLAUDE.deserialize(_load_fixture(SYNTHETIC_FIXTURE))
        signed_reasoning = next(e for e in entries if isinstance(e, Reasoning) and e.signature)
        # The wrapper-level model/error/parent_tool_use_id/uuid must reach
        # every block produced from the same assistant message.
        same_wrapper_text = next(
            e for e in entries if isinstance(e, AssistantText) and e.text == "The capital is Paris."
        )
        assert same_wrapper_text.model == "claude-opus-4-7"
        assert same_wrapper_text.error == "rate_limit"
        assert same_wrapper_text.parent_tool_use_id == "toolu_parent_subagent_42"
        assert same_wrapper_text.uuid == "asst-msg-003"
        # The thinking block from the same wrapper carries the wrapper
        # uuid in raw because Reasoning has no typed uuid field.
        assert signed_reasoning.raw.get("_message_extras", {}).get("id") == ("msg_synth_signed_001")

    def test_synthetic_tool_use_result_metadata_lifted(self) -> None:
        entries = CLAUDE.deserialize(_load_fixture(SYNTHETIC_FIXTURE))
        rich = next(
            e for e in entries if isinstance(e, ToolResult) and e.tool_use_result is not None
        )
        assert rich.tool_use_result == {
            "stdout": "4\n",
            "stderr": "",
            "exit_code": 0,
            "interrupted": False,
            "isImage": False,
        }
        assert rich.parent_tool_use_id == "toolu_synth_01"
        assert rich.uuid == "user-msg-004"

    def test_synthetic_task_lifecycle_emitted_in_order(self) -> None:
        entries = CLAUDE.deserialize(_load_fixture(SYNTHETIC_FIXTURE))
        lifecycle = [
            e for e in entries if isinstance(e, TaskStarted | TaskProgress | TaskNotification)
        ]
        kinds = [type(e).__name__ for e in lifecycle]
        assert kinds == [
            "TaskStarted",
            "TaskProgress",
            "TaskNotification",
            "TaskNotification",
        ]
        started = lifecycle[0]
        assert isinstance(started, TaskStarted)
        assert started.task_id == "task-synth-001"
        assert started.task_type == "research"
        assert started.session_id == "session-synth-abc"
        progress = lifecycle[1]
        assert isinstance(progress, TaskProgress)
        assert progress.last_tool_name == "mcp__openscientist-tools__web_search"
        assert progress.usage == {
            "total_tokens": 1234,
            "tool_uses": 3,
            "duration_ms": 5678,
        }
        completed = lifecycle[2]
        assert isinstance(completed, TaskNotification)
        assert completed.status == "completed"
        assert completed.usage is not None
        failed = lifecycle[3]
        assert isinstance(failed, TaskNotification)
        assert failed.status == "failed"
        assert failed.usage is None


class TestOnDiskTranscripts:
    """Optional sweep across a local jobs directory, named through
    OPENSCIENTIST_TEST_LIVE_JOBS_DIR."""

    @pytest.mark.skipif(
        LIVE_JOBS_DIR is None,
        reason=f"set {LIVE_JOBS_DIR_ENV} to a readable jobs directory to sweep its transcripts",
    )
    def test_every_shape_a_transcript_translates_without_raising(self) -> None:
        assert LIVE_JOBS_DIR is not None
        translated = 0
        for path in sorted(LIVE_JOBS_DIR.glob("*/provenance/iter*_transcript.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            # Only Shape-A: top-level entries with "type" + "message" keys.
            if not data or not isinstance(data[0], dict):
                continue
            if "type" not in data[0] or "message" not in data[0]:
                continue
            entries = CLAUDE.deserialize(data)
            # Every output entry validates as a TranscriptEntry.
            TranscriptAdapter.dump_python(entries, mode="json")
            translated += 1
        assert translated > 0, "Expected at least one Shape-A transcript under jobs/"

    def test_an_unreadable_jobs_directory_skips_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        locked = tmp_path / "locked"
        (locked / "jobs").mkdir(parents=True)
        locked.chmod(0)
        try:
            monkeypatch.setenv(LIVE_JOBS_DIR_ENV, str(locked / "jobs"))
            assert _live_jobs_dir() is None
        finally:
            locked.chmod(0o700)

    def test_no_jobs_directory_is_swept_unless_one_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(LIVE_JOBS_DIR_ENV, raising=False)
        assert _live_jobs_dir() is None
        monkeypatch.setenv(LIVE_JOBS_DIR_ENV, str(tmp_path))
        assert _live_jobs_dir() == tmp_path


# ---- Forward-compatibility extras ---------------------------------------------------------------


class TestForwardCompatibility:
    """Unconsumed source fields land in the produced entry's ``raw`` dict."""

    def test_block_with_extra_unknown_keys_lands_in_raw_block_extras(self) -> None:
        """Anthropic sometimes adds keys like ``cache_control`` on blocks."""
        out = CLAUDE.deserialize(
            [
                _assistant(
                    [
                        {
                            "type": "text",
                            "text": "hello",
                            "cache_control": {"type": "ephemeral"},
                            "future_field": 42,
                        }
                    ]
                )
            ]
        )
        assert isinstance(out[0], AssistantText)
        assert out[0].text == "hello"
        assert out[0].raw["_block_extras"] == {
            "cache_control": {"type": "ephemeral"},
            "future_field": 42,
        }

    def test_thinking_block_with_signature_lifts_to_typed_field(self) -> None:
        """``ThinkingBlock.signature`` lifts into the typed ``signature`` field
        on Reasoning so extended-thinking replay verification is preserved.
        """
        out = CLAUDE.deserialize(
            [
                _assistant(
                    [
                        {
                            "type": "thinking",
                            "thinking": "step-by-step",
                            "signature": "EhcKBnRvb2xfdQ==",
                        }
                    ]
                )
            ]
        )
        assert isinstance(out[0], Reasoning)
        assert out[0].text == "step-by-step"
        assert out[0].signature == "EhcKBnRvb2xfdQ=="

    def test_thinking_block_with_list_summary_keeps_list(self) -> None:
        out = CLAUDE.deserialize(
            [_assistant([{"type": "thinking", "thinking": "x", "summary": ["a", "b"]}])]
        )
        assert isinstance(out[0], Reasoning)
        assert out[0].summary == ["a", "b"]

    def test_assistant_message_model_lifts_to_typed_field(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "assistant",
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "content": [{"type": "text", "text": "ok"}],
                    },
                }
            ]
        )
        assert isinstance(out[0], AssistantText)
        assert out[0].model == "claude-sonnet-4-6"

    def test_assistant_message_error_lifts_to_typed_field(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "assistant",
                    "message": {
                        "error": "rate_limit",
                        "content": [{"type": "text", "text": "partial"}],
                    },
                }
            ]
        )
        assert isinstance(out[0], AssistantText)
        assert out[0].error == "rate_limit"

    def test_entry_parent_tool_use_id_lifts_to_typed_field(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "assistant",
                    "parent_tool_use_id": "toolu_parent_call",
                    "uuid": "msg_abc",
                    "message": {"content": [{"type": "text", "text": "from subagent"}]},
                }
            ]
        )
        assert isinstance(out[0], AssistantText)
        assert out[0].parent_tool_use_id == "toolu_parent_call"
        assert out[0].uuid == "msg_abc"

    def test_user_tool_use_result_lifts_onto_tool_result(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "user",
                    "tool_use_result": {
                        "type": "edit_file",
                        "stdout": "applied",
                        "exit_code": 0,
                    },
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_x",
                                "content": "ok",
                                "is_error": False,
                            }
                        ]
                    },
                }
            ]
        )
        assert isinstance(out[0], ToolResult)
        assert out[0].call_id == "toolu_x"
        assert out[0].output == "ok"
        assert out[0].success is True
        assert out[0].tool_use_result == {
            "type": "edit_file",
            "stdout": "applied",
            "exit_code": 0,
        }

    def test_multi_iteration_back_and_forth_translates_in_order(self) -> None:
        """Three turns of assistant->user with multiple calls per turn."""
        messages: list[dict[str, Any]] = []
        for turn in range(3):
            messages.append(
                _assistant(
                    [
                        {"type": "text", "text": f"turn {turn}"},
                        {
                            "type": "tool_use",
                            "id": f"t{turn}",
                            "name": "execute_code",
                            "input": {"code": f"print({turn})"},
                        },
                    ]
                )
            )
            messages.append(
                _user(
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"t{turn}",
                            "content": f"{turn}\n",
                            "is_error": False,
                        }
                    ]
                )
            )
        out = CLAUDE.deserialize(messages)
        assert len(out) == 9  # 3 turns * (text + call + result)
        assert [type(e).__name__ for e in out] == [
            "AssistantText",
            "ToolCall",
            "ToolResult",
            "AssistantText",
            "ToolCall",
            "ToolResult",
            "AssistantText",
            "ToolCall",
            "ToolResult",
        ]
        # Every result's call_id matches the immediately preceding tool call.
        for i in range(0, 9, 3):
            call = out[i + 1]
            result = out[i + 2]
            assert isinstance(call, ToolCall)
            assert isinstance(result, ToolResult)
            assert result.call_id == call.id


# ---- Task lifecycle variants ----------------------------------------------------------------------


class TestTaskLifecycle:
    """Task lifecycle system messages become typed entries."""

    def test_task_started_translates_with_all_fields(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "system",
                    "subtype": "task_started",
                    "task_id": "t-1",
                    "description": "Run the analysis",
                    "task_type": "code",
                    "tool_use_id": "toolu_parent",
                    "session_id": "sess-99",
                    "uuid": "msg-abc",
                }
            ]
        )
        assert len(out) == 1
        assert isinstance(out[0], TaskStarted)
        assert out[0].task_id == "t-1"
        assert out[0].description == "Run the analysis"
        assert out[0].task_type == "code"
        assert out[0].parent_tool_use_id == "toolu_parent"
        assert out[0].session_id == "sess-99"
        assert out[0].uuid == "msg-abc"
        assert out[0].raw == {}

    def test_task_progress_keeps_usage_dict_verbatim(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "system",
                    "subtype": "task_progress",
                    "task_id": "t-2",
                    "description": "Halfway done",
                    "last_tool_name": "execute_code",
                    "usage": {"total_tokens": 1234, "tool_uses": 3, "duration_ms": 5678},
                    "session_id": "sess-99",
                    "uuid": "msg-def",
                }
            ]
        )
        assert isinstance(out[0], TaskProgress)
        assert out[0].usage == {
            "total_tokens": 1234,
            "tool_uses": 3,
            "duration_ms": 5678,
        }
        assert out[0].last_tool_name == "execute_code"

    def test_task_notification_translates_completed_status(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "system",
                    "subtype": "task_notification",
                    "task_id": "t-3",
                    "status": "completed",
                    "summary": "Analysis finished",
                    "output_file": "/tmp/result.json",
                    "uuid": "msg-zzz",
                    "session_id": "sess-99",
                }
            ]
        )
        assert isinstance(out[0], TaskNotification)
        assert out[0].status == "completed"
        assert out[0].summary == "Analysis finished"
        assert out[0].output_file == "/tmp/result.json"

    def test_task_started_extra_keys_land_in_raw(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "system",
                    "subtype": "task_started",
                    "task_id": "t-1",
                    "description": "Run",
                    "future_field": {"forward": "compat"},
                }
            ]
        )
        assert isinstance(out[0], TaskStarted)
        assert out[0].raw == {"future_field": {"forward": "compat"}}


# ---- UserPrompt -----------------------------------------------------------------------------------


class TestUserPromptTranslation:
    def test_user_string_content_becomes_user_prompt(self) -> None:
        """``UserMessage.content`` can be a plain string (SDK-allowed)."""
        out = CLAUDE.deserialize([{"type": "user", "message": {"content": "operator prompt"}}])
        assert isinstance(out[0], UserPrompt)
        assert out[0].text == "operator prompt"

    def test_user_text_block_becomes_user_prompt_with_lifted_uuid(self) -> None:
        out = CLAUDE.deserialize(
            [
                {
                    "type": "user",
                    "uuid": "msg-user-1",
                    "parent_tool_use_id": "toolu_parent",
                    "message": {"content": [{"type": "text", "text": "go"}]},
                }
            ]
        )
        assert isinstance(out[0], UserPrompt)
        assert out[0].text == "go"
        assert out[0].uuid == "msg-user-1"
        assert out[0].parent_tool_use_id == "toolu_parent"


# ---- No-drop coverage property ------------------------------------------------------------------


def _flatten_source_keys(node: Any, prefix: str = "") -> list[str]:
    """Yield every leaf path of a nested dict/list, e.g.
    ``message.content[0].type`` so we can compare source vs translated coverage.
    """
    paths: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else key
            sub = _flatten_source_keys(value, child)
            if sub:
                paths.extend(sub)
            else:
                paths.append(child)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            sub = _flatten_source_keys(value, f"{prefix}[{index}]")
            if sub:
                paths.extend(sub)
            else:
                paths.append(f"{prefix}[{index}]")
    else:
        if prefix:
            paths.append(prefix)
    return paths


def _flatten_entry_values(node: Any) -> set[str]:
    """Flatten every leaf value in a TranscriptEntry serialised to JSON.

    Used by the no-drop coverage check below.
    """
    values: set[str] = set()
    if isinstance(node, dict):
        for value in node.values():
            values.update(_flatten_entry_values(value))
    elif isinstance(node, list):
        for value in node:
            values.update(_flatten_entry_values(value))
    else:
        if node is not None:
            values.add(repr(node))
    return values


class TestNoDropCoverage:
    """Every source leaf value appears in the translated entries (typed
    field or ``raw``). Compares values, not keys, to allow renames.
    """

    @pytest.mark.parametrize("path", sorted(FIXTURE_DIR.glob("*.json.gz")), ids=lambda p: p.stem)
    def test_real_fixture_loses_no_source_value(self, path: Path) -> None:
        messages = _load_fixture(path)
        entries = CLAUDE.deserialize(messages)
        # Serialise produced entries and collect all leaf values.
        produced = _flatten_entry_values(TranscriptAdapter.dump_python(entries, mode="json"))
        # Collect all source leaf values.
        source = _flatten_entry_values(messages)
        # Source ``type`` / ``subtype`` discriminators get renamed by
        # the translator and so do not surface verbatim.
        consumed_discriminators = {
            "'assistant'",
            "'user'",
            "'system'",
            "'text'",
            "'tool_use'",
            "'tool_result'",
            "'thinking'",
            "'task_started'",
            "'task_progress'",
            "'task_notification'",
        }
        missing = source - produced - consumed_discriminators
        assert not missing, (
            f"{path.name}: source values dropped by translator: {sorted(missing)[:10]}"
        )


# ---- UnknownEntry warning emission --------------------------------------------------------------


class TestUnknownEntryLogging:
    def test_unknown_block_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="openscientist.transcript"):
            CLAUDE.deserialize([_assistant([{"type": "future_block_kind", "data": "x"}])])
        assert any(
            "UnknownEntry" in rec.message and "future_block_kind" in rec.message
            for rec in caplog.records
        )

    def test_unknown_entry_type_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="openscientist.transcript"):
            CLAUDE.deserialize([{"type": "future_entry_kind", "payload": {}}])
        assert any(
            "UnknownEntry" in rec.message and "future_entry_kind" in rec.message
            for rec in caplog.records
        )

    def test_unknown_system_subtype_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING", logger="openscientist.transcript"):
            CLAUDE.deserialize([{"type": "system", "subtype": "future_phase"}])
        assert any("UnknownEntry" in rec.message for rec in caplog.records)


# ---- No UnknownEntry in any real fixture --------------------------------------------------------


class TestRealFixturesProduceNoUnknown:
    @pytest.mark.parametrize("path", sorted(FIXTURE_DIR.glob("*.json.gz")), ids=lambda p: p.stem)
    def test_real_fixture_has_no_unknown_entries(self, path: Path) -> None:
        # The synthetic fixture intentionally exercises the unknown path
        # by including blocks the translator doesn't recognise. Skip it.
        if path.stem.startswith("synthetic_"):
            pytest.skip("synthetic fixture is allowed to contain UnknownEntry")
        entries = CLAUDE.deserialize(_load_fixture(path))
        unknown = [e for e in entries if isinstance(e, UnknownEntry)]
        assert not unknown, (
            f"{path.name}: translator produced UnknownEntry. A real-data "
            f"block type is not recognised. Sample: {unknown[0].raw}"
        )


# ---- Coverage gate ------------------------------------------------------------------------------


# Variants we expect at least one REAL fixture to exercise. Locked
# here so that accidentally breaking real-data coverage for one of
# them surfaces as a test failure. Variants not in this list
# (TaskProgress, TaskNotification, AssistantMessage.error) currently
# have synthetic-only coverage.
_CLAUDE_REAL_FIXTURE_VARIANTS: list[type[Any]] = sorted(
    [
        AssistantText,
        ToolCall,
        ToolResult,
        Reasoning,
        UserPrompt,
        SessionInit,
        TaskStarted,
    ],
    key=lambda c: c.__name__,
)


@pytest.mark.parametrize(
    "variant",
    _CLAUDE_REAL_FIXTURE_VARIANTS,
    ids=lambda c: c.__name__,
)
def test_each_target_variant_has_at_least_one_real_fixture(variant: type[Any]) -> None:
    """A variant once exercised by a real fixture must stay covered."""
    for path in REAL_FIXTURES:
        entries = CLAUDE.deserialize(_load_fixture(path))
        if any(isinstance(e, variant) for e in entries):
            return
    pytest.fail(f"No real Claude fixture produces a {variant.__name__} entry.")
