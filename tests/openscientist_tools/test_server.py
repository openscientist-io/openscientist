"""End-to-end stdio tests for the standalone tools MCP server."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent


async def test_ping_listed_and_returns_state_bound_job_id(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    async with stdio_client(server_params(server_env(tmp_path))) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert "ping" in names
            ping_tool = next(t for t in tools.tools if t.name == "ping")
            assert "message" in ping_tool.inputSchema["properties"]

            result = await session.call_tool("ping", {"message": "world"})
            (block,) = result.content
            assert isinstance(block, TextContent)
            assert block.text == "pong: world from job test-job-001"


def test_missing_required_env_var_fails(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
) -> None:
    env = server_env(tmp_path, OPENSCIENTIST_JOB_DIR="")
    proc = subprocess.run(
        [sys.executable, "-m", "openscientist_tools"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0
    assert "job_dir" in proc.stderr.lower()
