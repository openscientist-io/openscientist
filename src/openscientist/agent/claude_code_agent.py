"""ClaudeCodeAgent, driving the claude-agent-sdk for Claude-compatible providers.

The SDK's bundled CLI routes to the provider's backend via
``provider.claude_sdk_env()`` applied to the subprocess environment.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
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
    TurnOutcome,
)
from openscientist.agent.mcp_specs import StdioMcpServerSpec
from openscientist.providers.base import ClaudeCompatible
from openscientist.transcript import CLAUDE, TranscriptEntry

if TYPE_CHECKING:
    from openscientist.database.models import Skill
    from openscientist.prompts.common import BackendFragments

logger = logging.getLogger(__name__)

#: The Messages API refuses a request over 32 MB, so a larger tool result
#: never reaches the model.
_API_REQUEST_MAX_BYTES = 32 * 1024 * 1024
#: The CLI writes a Read result into its stream-json line twice: as the tool
#: result for the model and as the raw result for the SDK.
_RESULT_COPIES_PER_MESSAGE = 2
#: The largest stream-json message the session reads: any tool result the API
#: could take, in both copies, with room for the message around them. A few
#: such messages fit well inside the agent container's memory limit.
MAX_MESSAGE_BYTES = _RESULT_COPIES_PER_MESSAGE * _API_REQUEST_MAX_BYTES + 1024 * 1024

#: Opens the prompt of a turn retried after a tool result outgrew the session.
OVERSIZED_RESULT_NOTE = (
    "Your previous attempt at this turn stopped because one tool result was too large "
    "to pass back to you. Do not Read large files directly: view an attached image "
    "through its copy in `previews/`, read documents with `read_document`, and analyse "
    "data files with `execute_code`."
)


class _OversizedMessageError(Exception):
    """The SDK dropped the session over a message past ``MAX_MESSAGE_BYTES``."""

    def __init__(self, tool_calls: int, transcript: list[TranscriptEntry]) -> None:
        super().__init__("a tool result passed the message bound")
        self.tool_calls = tool_calls
        self.transcript = transcript


# Monkey-patch: make the SDK's message parser tolerant of unknown types
# (e.g. rate_limit_event added in newer API versions). Without this the SDK
# raises MessageParseError and kills the entire agentic loop.
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
    """Agent that wraps the claude-agent-sdk ``ClaudeSDKClient``.

    Tools come from the standalone ``openscientist_tools`` package, spawned as a
    stdio subprocess MCP server. The client is connected lazily on the first
    ``run_iteration`` and kept alive across iterations. ``reset_session=True``
    starts a fresh session.
    """

    def __init__(self, config: AgentConfig, provider: ClaudeCompatible) -> None:
        super().__init__(config, provider)
        self._model_override = config.model_override
        self._client: ClaudeSDKClient | None = None
        self._stderr_lines: list[str] = []

    backend = AgentBackend.CLAUDE_CODE
    file_write_tool = "Write"
    display_name = "Claude Code"
    skills_subdir = ".claude/skills"

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
        # Claude's rich per-job CLAUDE.md is always written; skills follow.
        self._write_job_claude_md(use_hypotheses=use_hypotheses)
        await super().prepare_job_workspace(use_hypotheses=use_hypotheses)

    def _write_job_claude_md(self, *, use_hypotheses: bool = False) -> None:
        from openscientist.prompts import generate_job_claude_md
        from openscientist.settings import get_settings

        claude_dir = self._config.job_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        try:
            (claude_dir / "CLAUDE.md").write_text(
                generate_job_claude_md(
                    use_hypotheses=use_hypotheses,
                    phenix_available=get_settings().phenix.is_available,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Failed to write job CLAUDE.md: %s", e)

    def _write_skill(self, skills_root: Path, skill: Skill) -> None:
        # Flat ``<name>.md`` with a human header (the claude-agent-sdk layout).
        skills_root.mkdir(parents=True, exist_ok=True)
        header = f"# {skill.name}\n*Category: {skill.category}*\n"
        if skill.description:
            header += f"\n{skill.description}\n"
        path = skills_root / f"{skill.category}--{skill.slug}.md"
        path.write_text(header + "\n" + skill.content, encoding="utf-8")

    def apply_runtime_environment(self) -> None:
        # Auth/routing flags for the Claude CLI and the tools subprocess.
        self._provider.setup_environment()

    @classmethod
    def chat_model_override(cls) -> str | None:
        # ANTHROPIC_CHAT_MODEL escape hatch, routing chat to a different model
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
        """Auto-approve all tool use. The agent runs autonomously."""
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
        env = super()._subprocess_env(config.job_dir)
        if config.data_file is None:
            env.pop("OPENSCIENTIST_DATA_FILE", None)
        if not config.data_files:
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
            tools=["Read", "Grep", "Glob"] if self._config.chat else None,
            setting_sources=[] if self._config.chat else None,
            skills=[] if self._config.chat else None,
            env={"CLAUDE_CODE_DISABLE_ATTACHMENTS": "1"} if self._config.chat else {},
            mcp_servers={
                "openscientist-tools": cast(McpStdioServerConfig, spec.to_sdk_config()),
            },
            model=self._model_override or self._provider.claude_model_name(),
            can_use_tool=self._allow_all_tools,
            cwd=str(job_dir),
            stderr=self._stderr_callback,
            extra_args={"strict-mcp-config": None} if self._config.chat else {},
            max_buffer_size=MAX_MESSAGE_BYTES,
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
        """Run one iteration via the SDK's bidirectional client.

        A tool result past ``MAX_MESSAGE_BYTES`` ends the session. The turn
        then runs once more in a fresh session told to avoid the large file,
        and fails with a plain reason only when that overflows too.
        """
        try:
            return await self._run_turn(prompt, reset_session=reset_session)
        except _OversizedMessageError as first:
            logger.warning(
                "A tool result passed the %d byte message bound after %d tool calls; "
                "retrying the turn in a fresh session",
                MAX_MESSAGE_BYTES,
                first.tool_calls,
            )
        await self.shutdown()
        try:
            return await self._run_turn(f"{OVERSIZED_RESULT_NOTE}\n\n{prompt}", reset_session=True)
        except _OversizedMessageError as second:
            await self.shutdown()
            return IterationResult(
                outcome=TurnOutcome.FAILED,
                output="",
                tool_calls=second.tool_calls,
                transcript=second.transcript,
                error=(
                    "A tool result was too large for the agent session (over "
                    f"{MAX_MESSAGE_BYTES / (1024 * 1024):g} MiB in one message), "
                    "also after a retry in a fresh session."
                ),
            )

    async def _run_turn(self, prompt: str, *, reset_session: bool) -> IterationResult:
        await self._reset_session_if_requested(reset_session)
        self._stderr_lines.clear()
        state = _IterationState()

        try:
            client = await self._ensure_client()
            await client.query(prompt)
            async for message in client.receive_response():
                self._handle_stream_message(message, state)
        except Exception as e:
            if "exceeded maximum buffer size" in str(e):
                raise _OversizedMessageError(
                    state.tool_call_count, CLAUDE.deserialize(state.transcript)
                ) from e
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
