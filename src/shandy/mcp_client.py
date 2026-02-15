"""
MCP (Model Context Protocol) client for SHANDY.

Provides a client that:
1. Spawns MCP servers using stdio transport
2. Discovers tools via session.list_tools()
3. Converts MCP tool schemas to Anthropic ToolParam format
4. Routes tool calls via session.call_tool()
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

logger = logging.getLogger(__name__)


class MCPClient:
    """
    MCP client that manages connections to MCP servers.

    Handles tool discovery, schema conversion, and tool execution.
    """

    def __init__(self) -> None:
        """Initialize MCP client."""
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []
        self._read = None
        self._write = None

    @asynccontextmanager
    async def connect(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> AsyncIterator["MCPClient"]:
        """
        Connect to an MCP server via stdio.

        Args:
            command: Command to run (e.g., "python")
            args: Arguments for the command (e.g., ["-m", "shandy.mcp_server", ...])
            env: Environment variables for the subprocess
            timeout: Startup timeout in seconds

        Yields:
            Self with an active connection

        Example:
            async with mcp_client.connect("python", ["-m", "shandy.mcp_server"]) as client:
                tools = client.get_anthropic_tools()
        """
        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=env,
        )

        logger.info("Connecting to MCP server: %s %s", command, " ".join(args))

        async with stdio_client(server_params) as (read, write):
            self._read = read
            self._write = write

            async with ClientSession(read, write) as session:
                self._session = session

                # Initialize the session
                try:
                    result = await asyncio.wait_for(
                        session.initialize(),
                        timeout=timeout,
                    )
                    logger.info(
                        "MCP session initialized: protocol=%s, server=%s",
                        result.protocolVersion,
                        result.serverInfo.name if result.serverInfo else "unknown",
                    )
                except asyncio.TimeoutError:
                    logger.error("MCP server initialization timed out after %ss", timeout)
                    raise RuntimeError(f"MCP server initialization timed out after {timeout}s")

                # Discover tools
                await self._discover_tools()

                try:
                    yield self
                finally:
                    self._session = None
                    self._tools = []

    async def _discover_tools(self) -> None:
        """Discover available tools from the MCP server."""
        if not self._session:
            raise RuntimeError("Not connected to MCP server")

        result = await self._session.list_tools()
        self._tools = []

        for tool in result.tools:
            # Convert MCP tool schema to a simplified format
            tool_info = {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            self._tools.append(tool_info)
            logger.debug("Discovered MCP tool: %s", tool.name)

        logger.info("Discovered %d MCP tools", len(self._tools))

    def get_anthropic_tools(
        self,
        allowed_tools: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get tools in Anthropic API format.

        Args:
            allowed_tools: Optional list of tool names to include.
                          If None, all tools are included.

        Returns:
            List of tool definitions compatible with Anthropic's API

        Example:
            tools = client.get_anthropic_tools(["execute_code", "search_pubmed"])
            response = anthropic.messages.create(..., tools=tools)
        """
        anthropic_tools = []

        for tool in self._tools:
            name = tool["name"]

            # Filter by allowed tools if specified
            if allowed_tools is not None:
                # Check both full name and prefixed name
                full_name = f"mcp__shandy-tools__{name}"
                if name not in allowed_tools and full_name not in allowed_tools:
                    continue

            anthropic_tool = {
                "name": name,
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            anthropic_tools.append(anthropic_tool)

        return anthropic_tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str:
        """
        Call an MCP tool and return the result.

        Args:
            name: Tool name
            arguments: Tool arguments as a dictionary

        Returns:
            Tool result as a string

        Raises:
            RuntimeError: If not connected or tool call fails
        """
        if not self._session:
            raise RuntimeError("Not connected to MCP server")

        # Strip the mcp__shandy-tools__ prefix if present
        if name.startswith("mcp__shandy-tools__"):
            name = name[len("mcp__shandy-tools__") :]

        logger.debug("Calling MCP tool: %s with args: %s", name, arguments)

        try:
            result = await self._session.call_tool(name, arguments)

            # Extract text content from result
            if result.content:
                text_parts = []
                for content_block in result.content:
                    if hasattr(content_block, "text"):
                        text_parts.append(content_block.text)
                    elif hasattr(content_block, "data"):
                        # Binary data - convert to representation
                        text_parts.append(f"[Binary data: {len(content_block.data)} bytes]")
                return "\n".join(text_parts)
            return ""

        except Exception as e:
            logger.error("MCP tool call failed: %s - %s", name, e)
            raise RuntimeError(f"Tool call failed: {name} - {e}") from e

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Get the list of discovered tools."""
        return self._tools.copy()

    @property
    def is_connected(self) -> bool:
        """Check if connected to an MCP server."""
        return self._session is not None


def create_mcp_config(
    job_dir: Path,
    data_file: Path | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    """
    Create MCP configuration for a job.

    Args:
        job_dir: Path to job directory (must be container-internal path)
        data_file: Optional path to primary data file
        timeout: Server startup timeout in seconds

    Returns:
        MCP config dictionary
    """
    mcp_args = [
        "-m",
        "shandy.mcp_server",
        "--job-dir",
        str(job_dir),
    ]
    if data_file:
        mcp_args.extend(["--data-file", str(data_file)])

    return {
        "mcpServers": {
            "shandy-tools": {
                "command": "python",
                "args": mcp_args,
                "timeout": timeout,
            }
        }
    }


async def test_mcp_connection(job_dir: Path) -> bool:
    """
    Test MCP server connection.

    Args:
        job_dir: Path to job directory

    Returns:
        True if connection successful, False otherwise
    """
    client = MCPClient()
    try:
        async with client.connect(
            "python",
            ["-m", "shandy.mcp_server", "--job-dir", str(job_dir)],
            timeout=30,
        ):
            tools = client.get_anthropic_tools()
            logger.info("MCP test successful: %d tools available", len(tools))
            return True
    except Exception as e:
        logger.error("MCP test failed: %s", e)
        return False
