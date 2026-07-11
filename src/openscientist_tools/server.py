"""FastMCP server instance and tool registrations."""

from __future__ import annotations

import logging
import os

from mcp.server.fastmcp import FastMCP

from openscientist_tools.state import STATE

logger = logging.getLogger("openscientist_tools.server")

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

# --------------------------------------------------------- airgap MCP policy

_AIRGAP_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _airgap_mode_requested() -> bool:
    """Cheap, settings-free read of OPENSCIENTIST_AIR_GAPPED.

    Deliberately does not go through :func:`openscientist.settings.get_settings`
    -- that can raise (e.g. AirgapSettings' required-address validators) for
    reasons unrelated to whether the operator *intended* air-gap mode, and
    this function's whole job is to decide whether such a failure should be
    fatal (see :func:`_apply_airgap_policy`).
    """
    return os.environ.get("OPENSCIENTIST_AIR_GAPPED", "").strip().lower() in _AIRGAP_TRUTHY


def _load_settings_and_filter() -> list[str]:
    """Load settings and enforce the airgap MCP tool policy against the live
    registry. Returns the list of tool names removed."""
    from openscientist.airgap.mcp_filter import enforce_mcp_policy
    from openscientist.settings import get_settings

    settings = get_settings()
    return enforce_mcp_policy(mcp, settings)


def _apply_airgap_policy() -> None:
    """Enforce the airgap MCP tool allowlist at server startup.

    Codex Review-6 wiring (see mcp_filter.py's enforce_mcp_policy
    docstring): without this call anywhere in the actual server startup
    path, the policy declared by mcp_filter.py is dead code -- every tool
    stays registered regardless of settings.

    Fail-closed when air-gap mode was explicitly requested
    (OPENSCIENTIST_AIR_GAPPED set): if settings loading or policy
    enforcement raises, propagate the exception so the MCP server refuses
    to start rather than silently running unfiltered under a broken
    config. Fail-open when air-gap mode wasn't requested: policy
    enforcement isn't load-bearing for a non-airgap deployment, so a
    broken settings load there shouldn't take down tool registration.
    """
    if not _airgap_mode_requested():
        try:
            _load_settings_and_filter()
        except Exception:
            logger.warning(
                "MCP airgap policy enforcement skipped (settings load failed, "
                "non-airgap deployment)",
                exc_info=True,
            )
        return

    removed = _load_settings_and_filter()
    if removed:
        logger.info("Airgap MCP policy removed tools: %s", removed)
