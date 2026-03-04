"""
SDKAgentExecutor — uses the claude-agent-sdk for all providers.

The claude-agent-sdk provides automatic tool-use loops, built-in tools
(Bash, file read/write), and the full Claude Code capability set.

The provider's ``setup_environment()`` configures env vars so the SDK's
bundled CLI routes to the correct backend (Anthropic, CBORG, Vertex,
Bedrock, Foundry).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
)
from claude_agent_sdk.types import (
    PermissionResultAllow,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from open_scientist.agent.protocol import IterationResult, TokenUsage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Monkey-patch: make the SDK's message parser tolerant of unknown types
# (e.g. rate_limit_event added in newer API versions).  Without this the
# SDK raises MessageParseError and kills the entire agentic loop.
# ---------------------------------------------------------------------------
def _install_parse_message_patch() -> None:
    """Wrap ``claude_agent_sdk._internal.message_parser.parse_message``
    so that unknown message types return a lightweight sentinel instead of
    raising ``MessageParseError``.
    """
    import claude_agent_sdk._internal.message_parser as _mp
    from claude_agent_sdk._errors import MessageParseError

    if getattr(_mp.parse_message, "__open_scientist_tolerant_patch__", False):
        return

    _original_parse = _mp.parse_message
    known_types = {"user", "assistant", "system", "result", "stream_event"}

    def _tolerant_parse(data: Any) -> Any:
        try:
            return _original_parse(data)
        except MessageParseError:
            if isinstance(data, dict):
                msg_type = data.get("type")
                if isinstance(msg_type, str) and msg_type not in known_types:
                    logger.debug("Skipping unrecognised SDK message type: %s", msg_type)
                    # Return a lightweight object that receive_response will ignore
                    # (it only acts on ResultMessage / AssistantMessage / etc.)
                    return _Sentinel(msg_type)
            raise

    _tolerant_parse.__open_scientist_tolerant_patch__ = True  # type: ignore[attr-defined]
    _mp.parse_message = _tolerant_parse


class _Sentinel:
    """Placeholder yielded for unknown message types."""

    __slots__ = ("type",)

    def __init__(self, msg_type: str) -> None:
        self.type = msg_type


@dataclass
class _IterationState:
    """Mutable state captured while processing one SDK streaming response."""

    tool_call_count: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    final_output: str = ""


_install_parse_message_patch()


class SDKAgentExecutor:
    """
    AgentExecutor that wraps the claude-agent-sdk ClaudeSDKClient.

    Uses @tool-decorated Python callables (from open_scientist.tools) exposed
    via an in-process MCP server.  The SDK handles the agentic loop
    internally.

    The client is connected lazily on first ``run_iteration`` call and
    kept alive for conversation continuity across iterations.  Pass
    ``reset_session=True`` to disconnect and start a fresh session.

    """

    def __init__(
        self,
        job_dir: Path,
        data_file: Path | None,
        system_prompt: str | None,
        use_hypotheses: bool = False,
        data_files: list[Path] | None = None,
    ) -> None:
        from open_scientist.tools.registry import build_tool_list

        self._job_dir = job_dir
        self._data_file = data_file
        self._system_prompt = system_prompt
        self._tools = build_tool_list(
            job_dir, data_file, use_hypotheses=use_hypotheses, data_files=data_files
        )
        self._token_usage = TokenUsage()
        self._client: ClaudeSDKClient | None = None
        self._stderr_lines: list[str] = []

    @staticmethod
    async def _allow_all_tools(
        _tool_name: str,
        _tool_input: dict[str, Any],
        _context: ToolPermissionContext,
    ) -> PermissionResultAllow:
        """Auto-approve all tool use — agent runs autonomously."""
        return PermissionResultAllow()

    def _stderr_callback(self, line: str) -> None:
        """Capture CLI stderr output for error diagnostics."""
        line = line.rstrip()
        if line:
            self._stderr_lines.append(line)
            logger.debug("claude-cli stderr: %s", line)

    def _build_options(self) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with tools exposed via an in-process MCP server."""
        from open_scientist.settings import get_settings

        settings = get_settings()
        server = create_sdk_mcp_server("open_scientist-tools", tools=self._tools)

        # Pass OAuth beta header to the CLI when using OAuth token
        extra_args: dict[str, str | None] = {}

        return ClaudeAgentOptions(
            system_prompt=self._system_prompt,
            mcp_servers={"open_scientist-tools": server},
            model=settings.provider.anthropic_model,
            can_use_tool=self._allow_all_tools,
            cwd=str(self._job_dir),
            stderr=self._stderr_callback,
            extra_args=extra_args,
        )

    async def _ensure_client(self) -> ClaudeSDKClient:
        """Return a connected ClaudeSDKClient, creating one if needed."""
        if self._client is None:
            options = self._build_options()
            self._client = ClaudeSDKClient(options=options)
            await self._client.connect()
            logger.info("ClaudeSDKClient connected")
        return self._client

    async def _reset_session_if_requested(self, reset_session: bool) -> None:
        """Disconnect existing SDK client when caller requests a fresh session."""
        if reset_session and self._client is not None:
            await self._client.disconnect()
            self._client = None
            logger.info("Session reset — client disconnected")

    @staticmethod
    def _usage_from_payload(usage: object) -> TokenUsage:
        """Normalize SDK usage payloads (object or dict) to TokenUsage."""
        if isinstance(usage, dict):
            return TokenUsage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            )
        return TokenUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_creation_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )

    def _record_usage(self, message: object) -> None:
        """Accumulate token usage from any SDK message carrying usage info."""
        usage = getattr(message, "usage", None)
        if usage:
            self._token_usage += self._usage_from_payload(usage)

    @staticmethod
    def _tool_use_item(block: ToolUseBlock, tool_call_count: int) -> dict[str, object]:
        """Build transcript entry for a single tool call."""
        return {
            "type": "tool_use",
            "id": getattr(block, "id", f"tool_{tool_call_count}"),
            "name": block.name,
            "input": getattr(block, "input", {}),
        }

    def _handle_content_list(self, raw_content: list[object], state: _IterationState) -> None:
        """Convert SDK content blocks into transcript items."""
        content_items: list[dict[str, object]] = []
        for block in raw_content:
            if isinstance(block, TextBlock):
                state.final_output = block.text
                content_items.append({"type": "text", "text": block.text})
                continue
            if isinstance(block, ToolUseBlock):
                state.tool_call_count += 1
                logger.debug("Tool call: %s", block.name)
                content_items.append(self._tool_use_item(block, state.tool_call_count))
        if content_items:
            state.transcript.append({"type": "assistant", "message": {"content": content_items}})

    @staticmethod
    def _handle_content_text(raw_content: str, state: _IterationState) -> None:
        """Record plain-string message content in iteration transcript."""
        state.final_output = raw_content
        state.transcript.append(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": raw_content}]},
            }
        )

    def _handle_stream_message(self, message: object, state: _IterationState) -> None:
        """Process one streamed SDK message."""
        if isinstance(message, _Sentinel):
            return

        self._record_usage(message)

        if isinstance(message, ResultMessage):
            if message.result:
                state.final_output = message.result
            return

        raw_content = getattr(message, "content", None)
        if isinstance(raw_content, list):
            self._handle_content_list(raw_content, state)
        elif isinstance(raw_content, str):
            self._handle_content_text(raw_content, state)

    def _stderr_tail(self, limit: int = 20) -> str:
        """Return the last stderr lines captured from the CLI."""
        return "\n".join(self._stderr_lines[-limit:])

    def _iteration_failure_result(
        self, error: Exception, state: _IterationState
    ) -> IterationResult:
        """Build IterationResult for exceptions raised during iteration streaming."""
        stderr_tail = self._stderr_tail()
        if stderr_tail:
            message = f"{error}\nCLI stderr:\n{stderr_tail}"
            logger.error("SDK query failed: %s\nCLI stderr:\n%s", error, stderr_tail)
        else:
            message = str(error)
            logger.error("SDK query failed: %s", error, exc_info=True)
        self._stderr_lines.clear()
        self._client = None
        return IterationResult(
            success=False,
            output="",
            tool_calls=state.tool_call_count,
            transcript=state.transcript,
            error=message,
        )

    def _api_error_result(self, state: _IterationState) -> IterationResult | None:
        """Return failure result if CLI returned an API error as plain text."""
        if not state.final_output or "API Error:" not in state.final_output:
            return None
        if state.tool_call_count != 0:
            return None
        logger.error("CLI returned API error as output: %s", state.final_output[:500])
        return IterationResult(
            success=False,
            output=state.final_output,
            tool_calls=0,
            transcript=state.transcript,
            error=state.final_output,
        )

    def _silent_crash_result(self, state: _IterationState) -> IterationResult | None:
        """Return failure result for no-output/no-transcript CLI crashes."""
        if state.final_output or state.tool_call_count or state.transcript:
            return None
        stderr_tail = self._stderr_tail()
        self._stderr_lines.clear()
        error_message = "CLI produced no output (process may have crashed)"
        if stderr_tail:
            error_message += f"\nCLI stderr:\n{stderr_tail}"
        logger.error(error_message)
        self._client = None
        return IterationResult(
            success=False,
            output="",
            tool_calls=0,
            transcript=[],
            error=error_message,
        )

    async def run_iteration(
        self,
        prompt: str,
        *,
        reset_session: bool = False,
    ) -> IterationResult:
        """
        Run one iteration via the SDK's bidirectional client.

        The client stays connected across iterations for conversation
        continuity.  When reset_session=True, the old client is
        disconnected and a fresh one is created.
        """
        await self._reset_session_if_requested(reset_session)
        self._stderr_lines.clear()
        state = _IterationState()

        try:
            client = await self._ensure_client()
            await client.query(prompt)
            async for message in client.receive_response():
                self._handle_stream_message(message, state)
        except Exception as e:
            return self._iteration_failure_result(e, state)

        api_error = self._api_error_result(state)
        if api_error is not None:
            return api_error

        silent_crash = self._silent_crash_result(state)
        if silent_crash is not None:
            return silent_crash

        return IterationResult(
            success=True,
            output=state.final_output,
            tool_calls=state.tool_call_count,
            transcript=state.transcript,
            error="",
        )

    async def shutdown(self) -> None:
        """Disconnect the SDK client."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.debug("Error during client disconnect", exc_info=True)
            self._client = None
        logger.debug("SDKAgentExecutor shut down")

    @property
    def total_tokens(self) -> TokenUsage:
        return self._token_usage
