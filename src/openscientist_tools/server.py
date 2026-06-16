"""FastMCP server instance and tool registrations."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from openscientist_tools.state import STATE

mcp = FastMCP("openscientist-tools")


@mcp.tool()
def ping(message: str = "hello") -> str:
    """Round-trip smoke tool that echoes the job id from server state."""
    return f"pong: {message} from job {STATE.job_id}"


from openscientist_tools import (  # noqa: F401, E402
    code_exec,
    document,
    exomiser,
    job_meta,
    knowledge,
    phenix,
    pubmed,
)
