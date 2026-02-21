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
from pathlib import Path

from claude_agent_sdk import (  # type: ignore[import-not-found]
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    create_sdk_mcp_server,
)
from claude_agent_sdk.types import (  # type: ignore[import-not-found]
    PermissionResultAllow,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from shandy.agent.protocol import IterationResult, TokenUsage

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
    import claude_agent_sdk._internal.message_parser as _mp  # type: ignore[import-not-found]

    _original_parse = _mp.parse_message

    def _tolerant_parse(data: dict):  # type: ignore[no-untyped-def]
        try:
            return _original_parse(data)
        except Exception:
            msg_type = data.get("type", "unknown")
            logger.debug("Skipping unrecognised SDK message type: %s", msg_type)
            # Return a lightweight object that receive_response will ignore
            # (it only acts on ResultMessage / AssistantMessage / etc.)
            return _Sentinel(msg_type)

    _mp.parse_message = _tolerant_parse


class _Sentinel:
    """Placeholder yielded for unknown message types."""

    __slots__ = ("type",)

    def __init__(self, msg_type: str) -> None:
        self.type = msg_type


_install_parse_message_patch()


class SDKAgentExecutor:
    """
    AgentExecutor that wraps the claude-agent-sdk ClaudeSDKClient.

    Uses @tool-decorated Python callables (from shandy.tools) exposed
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
    ) -> None:
        from shandy.tools.registry import build_tool_list

        self._job_dir = job_dir
        self._data_file = data_file
        self._system_prompt = system_prompt
        self._tools = build_tool_list(job_dir, data_file)
        self._token_usage = TokenUsage()
        self._client: ClaudeSDKClient | None = None
        self._stderr_lines: list[str] = []

    @staticmethod
    async def _allow_all_tools(
        tool_name: str,
        tool_input: dict,
        context: ToolPermissionContext,
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
        from shandy.settings import get_settings

        settings = get_settings()
        server = create_sdk_mcp_server("shandy-tools", tools=self._tools)

        # Pass OAuth beta header to the CLI when using OAuth token
        extra_args: dict[str, str | None] = {}

        return ClaudeAgentOptions(
            system_prompt=self._system_prompt,
            mcp_servers={"shandy-tools": server},
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
        if reset_session and self._client is not None:
            await self._client.disconnect()
            self._client = None
            logger.info("Session reset — client disconnected")

        tool_call_count = 0
        transcript: list[dict] = []
        final_output = ""
        error = ""

        try:
            client = await self._ensure_client()
            await client.query(prompt)

            async for message in client.receive_response():
                # Skip sentinel objects from the parse_message patch
                if isinstance(message, _Sentinel):
                    continue

                # Accumulate token usage
                usage = getattr(message, "usage", None)
                if usage:
                    self._token_usage += TokenUsage(
                        input_tokens=getattr(usage, "input_tokens", 0),
                        output_tokens=getattr(usage, "output_tokens", 0),
                        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0),
                        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
                    )

                # Handle ResultMessage (final message in receive_response)
                if isinstance(message, ResultMessage):
                    if message.usage:
                        self._token_usage += TokenUsage(
                            input_tokens=message.usage.get("input_tokens", 0),
                            output_tokens=message.usage.get("output_tokens", 0),
                            cache_creation_tokens=message.usage.get(
                                "cache_creation_input_tokens", 0
                            ),
                            cache_read_tokens=message.usage.get("cache_read_input_tokens", 0),
                        )
                    if message.result:
                        final_output = message.result
                    continue

                # Extract text content
                raw_content = getattr(message, "content", None)
                if isinstance(raw_content, list):
                    for block in raw_content:
                        if isinstance(block, TextBlock):
                            final_output = block.text
                            transcript.append({"role": "assistant", "content": final_output})
                        elif isinstance(block, ToolUseBlock):
                            tool_call_count += 1
                            logger.debug("Tool call: %s", block.name)
                            transcript.append(
                                {"role": "tool", "name": block.name, "content": "..."}
                            )
                        # ThinkingBlock and others are silently skipped
                elif isinstance(raw_content, str):
                    final_output = raw_content
                    transcript.append({"role": "assistant", "content": raw_content})

        except Exception as e:  # noqa: BLE001
            # Include captured stderr in error for diagnostics
            stderr_tail = "\n".join(self._stderr_lines[-20:])
            if stderr_tail:
                error = f"{e}\nCLI stderr:\n{stderr_tail}"
                logger.error("SDK query failed: %s\nCLI stderr:\n%s", e, stderr_tail)
            else:
                error = str(e)
                logger.error("SDK query failed: %s", e, exc_info=True)
            self._stderr_lines.clear()
            # Client may be in a bad state — discard it
            self._client = None
            return IterationResult(
                success=False,
                output="",
                tool_calls=tool_call_count,
                transcript=transcript,
                error=error,
            )

        # Detect API errors returned as text (e.g. auth failures)
        if final_output and "API Error:" in final_output and tool_call_count == 0:
            logger.error("CLI returned API error as output: %s", final_output[:500])
            return IterationResult(
                success=False,
                output=final_output,
                tool_calls=0,
                transcript=transcript,
                error=final_output,
            )

        # Detect silent CLI crash: no output, no tool calls, no error
        if not final_output and tool_call_count == 0 and not transcript:
            stderr_tail = "\n".join(self._stderr_lines[-20:])
            self._stderr_lines.clear()
            err_msg = "CLI produced no output (process may have crashed)"
            if stderr_tail:
                err_msg += f"\nCLI stderr:\n{stderr_tail}"
            logger.error(err_msg)
            self._client = None  # Discard likely-broken client
            return IterationResult(
                success=False,
                output="",
                tool_calls=0,
                transcript=[],
                error=err_msg,
            )

        return IterationResult(
            success=True,
            output=final_output,
            tool_calls=tool_call_count,
            transcript=transcript,
            error="",
        )

    async def shutdown(self) -> None:
        """Disconnect the SDK client."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("Error during client disconnect", exc_info=True)
            self._client = None
        logger.debug("SDKAgentExecutor shut down")

    @property
    def total_tokens(self) -> TokenUsage:
        return self._token_usage
