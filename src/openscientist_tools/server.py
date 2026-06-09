"""FastMCP server instance and tool registrations."""

from __future__ import annotations

import logging
import os
from typing import Any, Callable

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


def _airgap_mode_requested() -> bool:
    """Cheap env read so we can distinguish 'airgap is on, policy must
    enforce' from 'airgap is off, no policy to apply'.

    Reading ``OPENSCIENTIST_AIR_GAPPED`` directly skips the full
    ``Settings`` construction (which would fail under a misconfigured env);
    the canonical truthy parse mirrors pydantic-settings' bool handling.
    """
    raw = os.environ.get("OPENSCIENTIST_AIR_GAPPED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _load_settings_and_filter() -> tuple[Any, Callable[[Any, Any], list[str]]]:
    """Helper extracted so tests can patch the deferred imports cleanly.

    Returns ``(settings, enforce_mcp_policy)``. Any exception inside is
    surfaced to the caller, which decides whether to fail-open (non-airgap)
    or fail-closed (airgap).
    """
    from openscientist.airgap.mcp_filter import enforce_mcp_policy
    from openscientist.settings import get_settings

    return get_settings(), enforce_mcp_policy


def _apply_airgap_policy() -> None:
    """Codex Review-6 wiring: in air-gap mode, remove tools the policy denies.

    Without this call, every tool registered by the module imports above
    remains in the MCP registry regardless of the operator's airgap
    configuration — the policy in ``airgap.mcp_filter`` was dead code.

    Codex Review-7 BUG #4 (fixed): the prior version silently fail-opened
    when ``get_settings()`` raised (e.g. under a misconfigured env), so the
    policy never enforced even in airgap mode. Now: if ``OPENSCIENTIST_AIR_GAPPED``
    is set, any failure in settings load or enforcement re-raises so the
    MCP server refuses to start. Operators see the misconfig immediately.
    Non-airgap deployments retain the silent best-effort fallback.
    """
    airgap_required = _airgap_mode_requested()
    try:
        settings, enforce_mcp_policy = _load_settings_and_filter()
    except Exception as exc:
        if airgap_required:
            logger.error(
                "airgap policy could not load settings — refusing to start the MCP "
                "server because OPENSCIENTIST_AIR_GAPPED is set: %s",
                exc,
            )
            raise
        logger.debug("airgap policy not applied (non-airgap deployment): %s", exc)
        return
    try:
        removed = enforce_mcp_policy(mcp, settings)
    except Exception as exc:
        if airgap_required:
            logger.error(
                "airgap policy enforcement failed — refusing to start the MCP "
                "server because OPENSCIENTIST_AIR_GAPPED is set: %s",
                exc,
            )
            raise
        logger.warning("airgap policy enforcement failed: %s", exc)
        return
    if removed:
        logger.info("airgap policy: removed %d MCP tool(s): %s", len(removed), sorted(removed))


_apply_airgap_policy()
