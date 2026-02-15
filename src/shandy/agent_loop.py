"""
SDK-based agent loop for SHANDY autonomous discovery.

Replaces the Docker-based agent_container_manager with a direct SDK approach
that uses the Anthropic API with tool calling.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from shandy.mcp_client import MCPClient
from shandy.prompts import get_system_prompt
from shandy.providers import get_provider

logger = logging.getLogger(__name__)


class AgentLoop:
    """
    SDK-based agent loop with MCP tool calling.

    Handles:
    - Message history management for session continuity
    - Tool discovery via MCP client
    - Agentic loop (send message -> tool calls -> tool results -> repeat)
    - Session reset for context window management
    """

    def __init__(
        self,
        mcp_command: str,
        mcp_args: list[str],
        mcp_env: dict[str, str] | None = None,
        system_prompt: str | None = None,
        allowed_tools: list[str] | None = None,
        max_tool_iterations: int = 50,
        mcp_timeout: float = 120.0,
    ) -> None:
        """
        Initialize agent loop.

        Args:
            mcp_command: Command to run MCP server (e.g., "python")
            mcp_args: Arguments for MCP server command
            mcp_env: Environment variables for MCP server
            system_prompt: System prompt for the agent
            allowed_tools: Optional list of allowed tool names
            max_tool_iterations: Maximum tool call iterations per run
            mcp_timeout: MCP server startup timeout in seconds
        """
        self.mcp_command = mcp_command
        self.mcp_args = mcp_args
        self.mcp_env = mcp_env
        self.system_prompt = system_prompt or get_system_prompt()
        self.allowed_tools = allowed_tools
        self.max_tool_iterations = max_tool_iterations
        self.mcp_timeout = mcp_timeout

        # Message history for session continuity
        self._messages: list[dict[str, Any]] = []

        # Tracking
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._iteration_count = 0

    async def run_iteration(
        self,
        prompt: str,
        reset_session: bool = False,
    ) -> dict[str, Any]:
        """
        Run a single discovery iteration.

        Args:
            prompt: User prompt for this iteration
            reset_session: If True, clear message history before running

        Returns:
            Dict with:
                - success: bool
                - output: str (assistant's final response text)
                - transcript: list (all messages exchanged)
                - tool_calls: int (number of tool calls made)
                - error: str (if failed)
        """
        if reset_session:
            self._messages = []
            logger.info("Session reset - starting fresh conversation")

        self._iteration_count += 1

        # Create MCP client and connect
        mcp_client = MCPClient()

        try:
            async with mcp_client.connect(
                self.mcp_command,
                self.mcp_args,
                env=self.mcp_env,
                timeout=self.mcp_timeout,
            ):
                return await self._run_agentic_loop(mcp_client, prompt)

        except Exception as e:
            logger.error("Agent loop failed: %s", e, exc_info=True)
            return {
                "success": False,
                "output": "",
                "transcript": self._messages.copy(),
                "tool_calls": 0,
                "error": str(e),
            }

    async def _run_agentic_loop(
        self,
        mcp_client: MCPClient,
        prompt: str,
    ) -> dict[str, Any]:
        """
        Run the agentic loop until completion or tool limit.

        Args:
            mcp_client: Connected MCP client
            prompt: User prompt

        Returns:
            Result dictionary
        """
        provider = get_provider()

        # Get tools from MCP server
        tools = mcp_client.get_anthropic_tools(self.allowed_tools)
        logger.info("Available tools: %d", len(tools))

        # Add user message
        self._messages.append({"role": "user", "content": prompt})

        tool_call_count = 0
        final_output = ""
        transcript = []

        for iteration in range(self.max_tool_iterations):
            logger.debug(
                "Agentic loop iteration %d/%d",
                iteration + 1,
                self.max_tool_iterations,
            )

            # Send message with tools
            response = await provider.send_message_with_tools(
                messages=self._messages,
                tools=tools,
                system=self.system_prompt,
                max_tokens=4096,
            )

            # Track usage
            usage = response.get("usage", {})
            self._total_input_tokens += usage.get("input_tokens", 0)
            self._total_output_tokens += usage.get("output_tokens", 0)

            # Add assistant response to history
            assistant_message = {"role": "assistant", "content": response["content"]}
            self._messages.append(assistant_message)
            transcript.append(assistant_message)

            stop_reason = response.get("stop_reason", "end_turn")
            logger.debug("Stop reason: %s", stop_reason)

            # Check if we're done
            if stop_reason == "end_turn":
                # Extract final text output
                final_output = self._extract_text(response["content"])
                break

            elif stop_reason == "tool_use":
                # Process tool calls
                tool_results = await self._process_tool_calls(
                    mcp_client,
                    response["content"],
                )
                tool_call_count += len(tool_results)

                # Add tool results to history
                self._messages.append({"role": "user", "content": tool_results})
                transcript.append({"role": "user", "content": tool_results})

            elif stop_reason == "max_tokens":
                logger.warning("Response hit max_tokens limit")
                final_output = self._extract_text(response["content"])
                break

            else:
                logger.warning("Unknown stop_reason: %s", stop_reason)
                final_output = self._extract_text(response["content"])
                break

        else:
            logger.warning("Reached max tool iterations (%d)", self.max_tool_iterations)

        return {
            "success": True,
            "output": final_output,
            "transcript": transcript,
            "tool_calls": tool_call_count,
            "error": "",
        }

    async def _process_tool_calls(
        self,
        mcp_client: MCPClient,
        content_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Process tool calls from assistant response.

        Args:
            mcp_client: Connected MCP client
            content_blocks: Content blocks from assistant response

        Returns:
            List of tool_result content blocks
        """
        tool_results = []

        for block in content_blocks:
            if block.get("type") != "tool_use":
                continue

            tool_id = block.get("id", "")
            tool_name = block.get("name", "")
            tool_input = block.get("input", {})

            logger.info("Tool call: %s", tool_name)
            logger.debug("Tool input: %s", json.dumps(tool_input)[:500])

            try:
                result = await mcp_client.call_tool(tool_name, tool_input)
                is_error = False
            except Exception as e:
                logger.error("Tool call failed: %s - %s", tool_name, e)
                result = f"Error: {e}"
                is_error = True

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result,
                    "is_error": is_error,
                }
            )

        return tool_results

    def _extract_text(self, content_blocks: list[dict[str, Any]]) -> str:
        """Extract text content from content blocks."""
        text_parts = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        return "\n".join(text_parts)

    def clear_session(self) -> None:
        """Clear message history."""
        self._messages = []

    @property
    def message_count(self) -> int:
        """Get number of messages in history."""
        return len(self._messages)

    @property
    def total_tokens(self) -> dict[str, int]:
        """Get total token usage."""
        return {
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
        }


def create_agent_loop(
    job_dir: Path,
    data_file: Path | None = None,
    allowed_tools: list[str] | None = None,
    system_prompt: str | None = None,
) -> AgentLoop:
    """
    Create an agent loop for a job.

    Args:
        job_dir: Path to job directory
        data_file: Optional path to primary data file
        allowed_tools: Optional list of allowed tool names
        system_prompt: Optional custom system prompt

    Returns:
        Configured AgentLoop instance
    """
    # Build MCP server command
    mcp_args = [
        "-m",
        "shandy.mcp_server",
        "--job-dir",
        str(job_dir),
    ]
    if data_file:
        mcp_args.extend(["--data-file", str(data_file)])

    # Get environment for MCP server
    mcp_env = os.environ.copy()  # noqa: env-ok

    return AgentLoop(
        mcp_command="python",
        mcp_args=mcp_args,
        mcp_env=mcp_env,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
    )
