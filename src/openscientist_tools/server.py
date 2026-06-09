"""FastMCP server instance and tool registrations."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from openscientist_tools.state import STATE

logger = logging.getLogger(__name__)

mcp = FastMCP("openscientist-tools")


@mcp.tool()
def ping(message: str = "hello") -> str:
    """Round-trip smoke tool that echoes the job id from server state."""
    return f"pong: {message} from job {STATE.job_id}"


from openscientist_tools import (  # noqa: F401, E402
    code_exec,
    document,
    job_meta,
    knowledge,
    phenix,
    pubmed,
)


def _apply_airgap_policy() -> None:
    """Codex Review-6 wiring: in air-gap mode, remove tools the policy denies.

    Without this call, every tool registered by the module imports above
    remains in the MCP registry regardless of the operator's airgap
    configuration — the policy in ``airgap.mcp_filter`` was dead code.

    Best-effort: failures in import or enforcement log a warning and let
    the server start with the full registry. The agent-side egress
    boundary still holds; this is defense-in-depth.
    """
    try:
        from openscientist.airgap.mcp_filter import enforce_mcp_policy
        from openscientist.settings import get_settings

        settings = get_settings()
    except Exception as exc:
        logger.debug("airgap policy not applied: %s", exc)
        return
    try:
        removed = enforce_mcp_policy(mcp, settings)
    except Exception as exc:
        logger.warning("airgap policy enforcement failed: %s", exc)
        return
    if removed:
        logger.info("airgap policy: removed %d MCP tool(s): %s", len(removed), sorted(removed))


_apply_airgap_policy()
