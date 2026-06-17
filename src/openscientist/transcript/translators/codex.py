"""Codex :class:`TranscriptDeserializer` backend.

Translates the ``ThreadItem`` objects produced by the official ``openai-codex``
SDK. ``CodexAgent`` dumps each item to its wire dict and wraps it in an
``item.completed`` envelope (see ``codex_agent._to_transcript``). This module
maps those item shapes (discriminated by their camelCase ``type``) to
:class:`TranscriptEntry` variants. The thread/turn envelope handlers are kept
for callers that feed a full event stream.
"""

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from openscientist.transcript.translators.helpers import (
    block_extras_or_none,
    merge_overlay,
    unknown,
    unknown_block,
)
from openscientist.transcript.union import TranscriptEntry
from openscientist.transcript.variants import (
    AssistantText,
    CollabAgentToolCall,
    FileChange,
    Plan,
    Reasoning,
    ShellExecution,
    TaskNotification,
    TaskStarted,
    ToolCall,
    ToolResult,
    UserPrompt,
    WebSearch,
)

# ---------------------------------------------------------------------------
# Item-level context (passed to each item's translate method)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CodexItemContext:
    """Stream metadata threaded into each item's translate call."""

    thread_id: str | None
    overlay: dict[str, Any]


# ---------------------------------------------------------------------------
# Item-level Pydantic models (discriminated by ``type``)
# ---------------------------------------------------------------------------


class _CodexItem(BaseModel):
    """Base for Codex SDK thread items."""

    model_config = ConfigDict(extra="allow")
    id: str

    def model_extras(self) -> dict[str, Any]:
        return dict(self.__pydantic_extra__ or {})

    def _raw(self, ctx: _CodexItemContext) -> dict[str, Any]:
        return merge_overlay(ctx.overlay, block_extras_or_none(self.model_extras()))


class _UserMessageItem(_CodexItem):
    """User-supplied prompt. ``content`` is a list of input blocks. The text
    blocks carry the prompt."""

    type: Literal["userMessage"]
    content: list[dict[str, Any]] = Field(default_factory=list)

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        parts = [
            block["text"]
            for block in self.content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return [UserPrompt(id=self.id, text="\n".join(parts), raw=self._raw(ctx))]


class _AgentMessageItem(_CodexItem):
    type: Literal["agentMessage"]
    text: str

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        return [AssistantText(id=self.id, text=self.text, raw=self._raw(ctx))]


class _ReasoningItem(_CodexItem):
    """Reasoning item. Text lives in ``summary`` (falling back to ``content``),
    both lists of strings."""

    type: Literal["reasoning"]
    summary: list[str] | None = None
    content: list[str] | None = None

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        text = "\n".join(self.summary or self.content or [])
        return [Reasoning(id=self.id, text=text, raw=self._raw(ctx))]


class _CommandExecutionItem(_CodexItem):
    type: Literal["commandExecution"]
    command: str
    aggregated_output: str | None = None
    exit_code: int | None = None
    status: str | None = None

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        return [
            ShellExecution(
                id=self.id,
                command=self.command,
                output=self.aggregated_output or "",
                exit_code=self.exit_code,
                status=self.status,
                raw=self._raw(ctx),
            )
        ]


class _FileUpdateChange(BaseModel):
    model_config = ConfigDict(extra="allow")
    path: str
    # PatchChangeKind: an object like ``{"type": "add"|"delete"|"update"}``.
    kind: Any = None


_FILE_CHANGE_KIND_TO_VARIANT: dict[str, str] = {
    "add": "create",
    "delete": "delete",
    "update": "edit",
}


def _change_kind(kind: Any) -> str | None:
    """Extract the kind discriminator from a PatchChangeKind value, tolerating
    both the object form (``{"type": "add"}``) and a bare string."""
    if isinstance(kind, dict):
        value = kind.get("type")
        return value if isinstance(value, str) else None
    if isinstance(kind, str):
        return kind
    return None


class _FileChangeItem(_CodexItem):
    type: Literal["fileChange"]
    changes: list[_FileUpdateChange] = Field(default_factory=list)
    status: str | None = None  # PatchApplyStatus

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        if not self.changes:
            return [unknown_block("codex", ctx.overlay, self, "fileChange item has no changes")]
        success = self.status == "completed"
        out: list[TranscriptEntry] = []
        for change in self.changes:
            raw_kind = _change_kind(change.kind)
            kind = _FILE_CHANGE_KIND_TO_VARIANT.get(raw_kind) if raw_kind is not None else None
            if kind is None:
                out.append(
                    unknown(
                        "codex",
                        {
                            "_block": {
                                **self.model_dump(mode="json"),
                                "_offending_change": change.model_dump(mode="json"),
                            },
                            "_overlay": ctx.overlay,
                        },
                        f"fileChange kind {change.kind!r} has no FileChange variant",
                    )
                )
                continue
            out.append(
                FileChange(
                    id=self.id,
                    path=change.path,
                    kind=kind,  # type: ignore[arg-type]  # validated above
                    success=success,
                    status=self.status,
                    raw=self._raw(ctx),
                )
            )
        return out


class _McpToolCallItemResult(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    content: list[Any] = Field(default_factory=list)
    structured_content: Any | None = None


class _McpToolCallItemError(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: str


class _McpToolCallItem(_CodexItem):
    type: Literal["mcpToolCall"]
    server: str
    tool: str
    arguments: Any = None
    result: _McpToolCallItemResult | None = None
    error: _McpToolCallItemError | None = None
    status: str | None = None

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        # Emit ToolCall + ToolResult paired on id.
        args = self.arguments if isinstance(self.arguments, dict) else {}
        raw = self._raw(ctx)
        if self.arguments is not None and not isinstance(self.arguments, dict):
            raw = dict(raw)
            raw["_arguments_non_dict"] = self.arguments
        out: list[TranscriptEntry] = [
            ToolCall(
                id=self.id,
                tool=self.tool,
                arguments=args,
                server=self.server,
                raw=raw,
            )
        ]
        success = self.error is None and self.status == "completed"
        output_text = ""
        structured: Any | None = None
        content_items: list[Any] | None = None
        if self.result is not None:
            structured = self.result.structured_content
            content_items = [
                dict(item) if isinstance(item, dict) else item for item in self.result.content
            ]
            parts: list[str] = []
            for item in self.result.content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            output_text = "\n".join(parts)
        out.append(
            ToolResult(
                call_id=self.id,
                output=output_text,
                success=success,
                status=self.status,
                structured_content=structured,
                content_items=content_items,
                error_message=self.error.message if self.error else None,
                raw=raw,
            )
        )
        return out


class _DynamicToolCallItem(_CodexItem):
    """A generic (non-MCP) tool call, e.g. a built-in dynamic tool."""

    type: Literal["dynamicToolCall"]
    tool: str
    arguments: Any = None
    content_items: list[Any] | None = None
    namespace: str | None = None
    status: str | None = None

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        args = self.arguments if isinstance(self.arguments, dict) else {}
        raw = self._raw(ctx)
        if self.arguments is not None and not isinstance(self.arguments, dict):
            raw = dict(raw)
            raw["_arguments_non_dict"] = self.arguments
        out: list[TranscriptEntry] = [
            ToolCall(
                id=self.id,
                tool=self.tool,
                arguments=args,
                server=self.namespace,
                raw=raw,
            )
        ]
        success = self.status == "completed"
        parts: list[str] = []
        for item in self.content_items or []:
            if isinstance(item, dict) and item.get("type") in ("text", "inputText"):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        out.append(
            ToolResult(
                call_id=self.id,
                output="\n".join(parts),
                success=success,
                status=self.status,
                content_items=self.content_items,
                raw=raw,
            )
        )
        return out


class _CollabToolCallItem(_CodexItem):
    type: Literal["collabAgentToolCall"]
    prompt: str | None = None
    agents_states: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        return [
            CollabAgentToolCall(
                id=self.id,
                prompt=self.prompt,
                agents_states={k: v for k, v in self.agents_states.items()},
                raw=self._raw(ctx),
            )
        ]


class _WebSearchItem(_CodexItem):
    type: Literal["webSearch"]
    query: str
    action: dict[str, Any] | None = None

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        return [
            WebSearch(
                id=self.id,
                query=self.query,
                action=self.action,
                raw=self._raw(ctx),
            )
        ]


class _PlanItem(_CodexItem):
    """Running plan, rendered as text."""

    type: Literal["plan"]
    text: str

    def translate(self, ctx: _CodexItemContext) -> list[TranscriptEntry]:
        return [Plan(id=self.id, text=self.text, raw=self._raw(ctx))]


_CodexItemUnion = Annotated[
    _UserMessageItem
    | _AgentMessageItem
    | _ReasoningItem
    | _CommandExecutionItem
    | _FileChangeItem
    | _McpToolCallItem
    | _DynamicToolCallItem
    | _CollabToolCallItem
    | _WebSearchItem
    | _PlanItem,
    Field(discriminator="type"),
]
_CodexItemAdapter: TypeAdapter[_CodexItemUnion] = TypeAdapter(_CodexItemUnion)


# ---------------------------------------------------------------------------
# Envelope-level key sets (no-drop accounting)
# ---------------------------------------------------------------------------

_THREAD_STARTED_CONSUMED: frozenset[str] = frozenset({"type", "thread_id"})
_TURN_COMPLETED_CONSUMED: frozenset[str] = frozenset({"type", "usage"})
_TURN_FAILED_CONSUMED: frozenset[str] = frozenset({"type", "error"})
_ERROR_CONSUMED: frozenset[str] = frozenset({"type", "message"})


def _leftover(d: dict[str, Any], consumed: frozenset[str]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if k not in consumed}


# ---------------------------------------------------------------------------
# CodexDeserializer
# ---------------------------------------------------------------------------


class CodexDeserializer:
    """:class:`TranscriptDeserializer` for Codex SDK items wrapped in
    ``item.completed`` envelopes. Stateless. See :data:`CODEX` for the
    canonical instance.
    """

    def deserialize(self, raw: list[dict[str, Any]]) -> list[TranscriptEntry]:
        out: list[TranscriptEntry] = []
        thread_id: str | None = None
        for event in raw:
            if not isinstance(event, dict):
                out.append(
                    unknown(
                        "codex",
                        {"_non_dict_event": repr(event)},
                        "non-dict source event",
                    )
                )
                continue
            etype = event.get("type")
            match etype:
                case "thread.started":
                    new_thread_id = event.get("thread_id")
                    thread_id = new_thread_id if isinstance(new_thread_id, str) else None
                    out.append(
                        TaskStarted(
                            task_id=thread_id or "",
                            description="codex thread started",
                            task_type="thread",
                            session_id=thread_id,
                            raw=_leftover(event, _THREAD_STARTED_CONSUMED),
                        )
                    )
                case "turn.started":
                    continue
                case "item.started" | "item.updated":
                    # Superseded by item.completed.
                    continue
                case "item.completed":
                    out.extend(self._translate_item_event(event, thread_id))
                case "turn.completed":
                    usage = event.get("usage")
                    out.append(
                        TaskNotification(
                            task_id=thread_id or "",
                            status="completed",
                            summary="",
                            output_file="",
                            usage=usage if isinstance(usage, dict) else None,
                            session_id=thread_id,
                            raw=_leftover(event, _TURN_COMPLETED_CONSUMED),
                        )
                    )
                case "turn.failed":
                    err = event.get("error")
                    message = ""
                    if isinstance(err, dict):
                        candidate = err.get("message")
                        if isinstance(candidate, str):
                            message = candidate
                    out.append(
                        TaskNotification(
                            task_id=thread_id or "",
                            status="failed",
                            summary=message,
                            output_file="",
                            session_id=thread_id,
                            raw=_leftover(event, _TURN_FAILED_CONSUMED),
                        )
                    )
                case "error":
                    message = ""
                    candidate = event.get("message")
                    if isinstance(candidate, str):
                        message = candidate
                    out.append(
                        TaskNotification(
                            task_id=thread_id or "",
                            status="failed",
                            summary=message,
                            output_file="",
                            session_id=thread_id,
                            raw=_leftover(event, _ERROR_CONSUMED),
                        )
                    )
                case other:
                    out.append(
                        unknown(
                            "codex",
                            dict(event),
                            f"unrecognised event type {other!r}",
                        )
                    )
        return out

    def _translate_item_event(
        self, event: dict[str, Any], thread_id: str | None
    ) -> list[TranscriptEntry]:
        raw_item = event.get("item")
        if not isinstance(raw_item, dict):
            return [
                unknown(
                    "codex",
                    dict(event),
                    "item.completed event missing dict-shaped item",
                )
            ]
        envelope_extras = {k: v for k, v in event.items() if k not in ("type", "item")}
        overlay: dict[str, Any] = {}
        if thread_id is not None:
            overlay["_thread_id"] = thread_id
        if envelope_extras:
            overlay["_envelope_extras"] = envelope_extras
        ctx = _CodexItemContext(thread_id=thread_id, overlay=overlay)
        try:
            item = _CodexItemAdapter.validate_python(raw_item)
        except ValidationError as exc:
            return [
                unknown(
                    "codex",
                    {"_item": raw_item, "_overlay": overlay},
                    f"unrecognised item type {raw_item.get('type')!r}: {exc.errors()[0]['msg']}",
                )
            ]
        return item.translate(ctx)


CODEX: CodexDeserializer = CodexDeserializer()
"""Canonical :class:`TranscriptDeserializer` for the Codex SDK item wire
format."""
