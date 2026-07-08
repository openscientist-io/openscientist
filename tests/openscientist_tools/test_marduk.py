"""Registration-gating tests for the MARDUK tools MCP module.

Spawns the standalone tools server with/without MARDUK mode and checks which
tools are exposed. The tools themselves hit the DB / Monarch at call time, so
these tests only assert the gating (tool presence + input schema).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

_MARDUK_TOOLS = {
    "search_monarch",
    "monarch_associations",
    "monarch_entity",
    "remember_finding",
    "recall_memory",
}


async def test_marduk_tools_present_when_enabled(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    env = server_env(tmp_path, OPENSCIENTIST_MARDUK_ENABLED="1")
    async with stdio_client(server_params(env)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert _MARDUK_TOOLS <= names
            search = next(
                t for t in (await session.list_tools()).tools if t.name == "search_monarch"
            )
            assert "query" in search.inputSchema["properties"]


async def test_marduk_tools_absent_by_default(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    # No OPENSCIENTIST_MARDUK_ENABLED -> gated off.
    async with stdio_client(server_params(server_env(tmp_path))) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            assert _MARDUK_TOOLS.isdisjoint(names)
            assert "ping" in names  # sanity: server is up
