"""
ClaudeCodeAgent — drives the claude-agent-sdk for Claude-compatible providers.

The claude-agent-sdk provides automatic tool-use loops, built-in tools
(Bash, file read/write), and the full Claude Code capability set.

The agent sources its model from ``provider.claude_model_name()`` and
applies ``provider.claude_sdk_env()`` to the environment so the SDK's
bundled CLI routes to the correct backend (Anthropic, CBORG, Vertex,
Bedrock, Foundry).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)
from claude_agent_sdk.types import (
    McpStdioServerConfig,
    PermissionResultAllow,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from openscientist.agent.base import (
    AbstractAgent,
    AgentBackend,
    AgentConfig,
    IterationResult,
    TokenUsage,
    TurnOutcome,
)
from openscientist.agent.mcp_specs import StdioMcpServerSpec
from openscientist.providers.base import ClaudeCompatible
from openscientist.transcript import CLAUDE

if TYPE_CHECKING:
    from openscientist.prompts.common import BackendFragments

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

    if getattr(_mp.parse_message, "__openscientist_tolerant_patch__", False):
        return

    _original_parse = _mp.parse_message
    known_types = {"user", "assistant", "system", "result", "stream_event"}

    def _tolerant_parse(data: Any) -> Any:
        try:
            result = _original_parse(data)
        except MessageParseError:
            if isinstance(data, dict):
                msg_type = data.get("type")
                if isinstance(msg_type, str) and msg_type not in known_types:
                    logger.debug("Skipping unrecognised SDK message type: %s", msg_type)
                    return _Sentinel(msg_type)
            raise
        # SDK >=0.1.46 returns None for unknown types instead of raising.
        if result is None and isinstance(data, dict):
            msg_type = data.get("type")
            if isinstance(msg_type, str) and msg_type not in known_types:
                logger.debug("Skipping unrecognised SDK message type: %s", msg_type)
                return _Sentinel(msg_type)
        return result

    _tolerant_parse.__openscientist_tolerant_patch__ = True  # type: ignore[attr-defined]
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


class ClaudeCodeAgent(AbstractAgent[ClaudeCompatible]):
    """
    Agent that wraps the claude-agent-sdk ClaudeSDKClient.

    Tools come from the standalone ``openscientist_tools`` package, spawned
    as a stdio subprocess MCP server that the SDK manages. The SDK handles
    the agentic loop internally.

    The client is connected lazily on first ``run_iteration`` call and
    kept alive for conversation continuity across iterations.  Pass
    ``reset_session=True`` to disconnect and start a fresh session.

    ``AgentConfig.model_override`` lets callers route a single run to a
    different model than the provider's default (used by in-page chat).
    """

    def __init__(self, config: AgentConfig, provider: ClaudeCompatible) -> None:
        super().__init__(config, provider)
        self._model_override = config.model_override
        self._client: ClaudeSDKClient | None = None
        self._stderr_lines: list[str] = []

    backend = AgentBackend.CLAUDE_CODE
    file_write_tool = "Write"

    @classmethod
    def prompt_fragments(cls) -> BackendFragments:
        from openscientist.prompts.claude import CLAUDE_FRAGMENTS

        return CLAUDE_FRAGMENTS

    @classmethod
    def discovery_system_prompt(
        cls, *, use_hypotheses: bool = False, phenix_available: bool = False
    ) -> str:
        # Claude gets the concise system prompt. Its rich CLAUDE.md is written
        # separately into .claude/ by prepare_job_workspace.
        return cls.system_prompt()

    async def prepare_job_workspace(self, *, use_hypotheses: bool = False) -> None:
        from openscientist.agent.skills import write_skills_to_claude_dir

        await write_skills_to_claude_dir(self._config.job_dir, use_hypotheses=use_hypotheses)

    def apply_runtime_environment(self) -> None:
        # Auth/routing flags for the Claude CLI and the tools subprocess.
        self._provider.setup_environment()

    @classmethod
    def chat_system_prompt(cls, base_system_prompt: str) -> str:
        # Claude reads chat guidance from .claude/CLAUDE.md (written by
        # write_chat_context), so the system prompt is the base unchanged.
        return base_system_prompt

    def write_chat_context(self) -> None:
        # Identity substitution keeps the file content the packaged template.
        from openscientist.prompts.common import render_chat_context

        claude_dir = self._config.job_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "CLAUDE.md").write_text(
            render_chat_context(self.prompt_fragments()), encoding="utf-8"
        )

    @classmethod
    def chat_model_override(cls) -> str | None:
        # ANTHROPIC_CHAT_MODEL escape hatch: route chat to a different model
        # than discovery (e.g. when the discovery model rejects chat prompts).
        from openscientist.settings import get_settings

        provider_settings = get_settings().provider
        return provider_settings.anthropic_chat_model or provider_settings.model

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

    def _build_subprocess_env(self) -> dict[str, str]:
        """Merge `os.environ` with per-job overlays for the standalone MCP.

        `subprocess.Popen` replaces env entirely when given a dict, so the
        spec must carry the full map (PATH, DATABASE_URL, etc. inherited
        from the agent container plus the per-job overlays).
        """
        config = self._config
        env = dict(os.environ)
        env["OPENSCIENTIST_JOB_ID"] = config.job_dir.name
        env["OPENSCIENTIST_JOB_DIR"] = str(config.job_dir)
        env["OPENSCIENTIST_USE_HYPOTHESES"] = "1" if config.use_hypotheses else "0"
        if config.data_file is not None:
            env["OPENSCIENTIST_DATA_FILE"] = str(config.data_file)
        else:
            env.pop("OPENSCIENTIST_DATA_FILE", None)
        if config.data_files:
            env["OPENSCIENTIST_DATA_FILES"] = os.pathsep.join(str(p) for p in config.data_files)
        else:
            env.pop("OPENSCIENTIST_DATA_FILES", None)
        return env

    def _build_options(self) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions with tools exposed via the standalone
        `openscientist_tools` subprocess MCP server."""
        job_dir = self._config.job_dir

        spec = StdioMcpServerSpec(
            name="openscientist-tools",
            command=sys.executable,
            args=("-m", "openscientist_tools"),
            env=self._build_subprocess_env(),
            cwd=str(job_dir),
        )

        return ClaudeAgentOptions(
            system_prompt=self._config.system_prompt,
            mcp_servers={
                "openscientist-tools": cast(McpStdioServerConfig, spec.to_sdk_config()),
            },
            model=self._model_override or self._provider.claude_model_name(),
            can_use_tool=self._allow_all_tools,
            cwd=str(job_dir),
            stderr=self._stderr_callback,
            extra_args={},
        )

    def _apply_provider_env(self) -> None:
        """Apply the provider's required auth/routing env vars to the process
        environment so the SDK CLI and the tools subprocess inherit them."""
        os.environ.update(self._provider.claude_sdk_env())  # env-ok

    async def _ensure_client(self) -> ClaudeSDKClient:
        """Return a connected ClaudeSDKClient, creating one if needed."""
        if self._client is None:
            self._apply_provider_env()
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
        """Normalize Anthropic SDK usage payloads (object or dict) to TokenUsage.

        Anthropic's shape is already additive: ``input_tokens`` excludes
        cached input, and ``cache_creation_input_tokens`` /
        ``cache_read_input_tokens`` are independent buckets. We pass the
        values through with only the field renames required by the
        normalized schema.

        ``reasoning_tokens`` is always 0 on this path because the
        Anthropic API does not expose a separate count for extended-
        thinking tokens; they are billed inside ``output_tokens``.

        The Codex backend (Phase 5) will have its own
        ``_usage_from_payload`` that subtracts ``cached_input_tokens``
        and ``reasoning_output_tokens`` from their parent totals to
        produce the same additive form.
        """
        if isinstance(usage, dict):
            return TokenUsage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_write_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            )
        return TokenUsage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
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
            outcome=TurnOutcome.FAILED,
            output="",
            tool_calls=state.tool_call_count,
            transcript=CLAUDE.deserialize(state.transcript),
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
            outcome=TurnOutcome.FAILED,
            output=state.final_output,
            tool_calls=0,
            transcript=CLAUDE.deserialize(state.transcript),
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
            outcome=TurnOutcome.FAILED,
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
            outcome=TurnOutcome.COMPLETED,
            output=state.final_output,
            tool_calls=state.tool_call_count,
            transcript=CLAUDE.deserialize(state.transcript),
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
        logger.debug("ClaudeCodeAgent shut down")
