"""Air-gap tool-allowlist policy for MCP + agent-SDK built-in tools.

Per RFC §10.1 (MCP tool gating) and §10.3 (Claude SDK built-in gating),
the air-gap mode declares — for every tool the agent could call — whether
it is safe to expose, whether it needs the operator to route it through
an internal endpoint, or whether it must be disabled outright.

The actual *enforcement* happens at three layers:

1. **Codex CLI fork** gates ``web_search`` at the source for non-OpenAI
   providers (Ollama, BedrockOpenAI, AzureOpenAI). No runtime filter needed
   for the Codex path — see ``providers/ollama.py`` and RFC §10.4.
2. **Kernel network-namespace isolation** on the executor container kills
   any ``import socket`` / ``urllib.request`` bypass that an unfiltered
   ``execute_code`` could otherwise do — see RFC §10.2 and
   ``container_manager.py:client``.
3. **Host firewall** + per-job internal Docker network — see RFC §6 and
   ``airgap/firewall.py`` (PR-2; out of scope here).

This module is the *declaration* those enforcers read from. Future
``AirgapClaudeCodeAgent`` (PR-2) will consult :func:`disallowed_claude_builtins`
when constructing its ``ClaudeAgentOptions``; a future runtime gate in the
MCP server can consult :func:`allowed_mcp_tools` to refuse registration of
banned tools at startup.

What this module does NOT do
----------------------------

* It does not silently allow a network-dependent tool just because its
  backend happens to be allowlisted. Operators who want ``search_pubmed``
  in air-gap mode have to set ``OPENSCIENTIST_AIRGAP_PUBMED_ADDR``; the
  default is denied.
* It does not introspect the live MCP registration to detect new tools
  that haven't been classified. :func:`unclassified_mcp_tools` does that
  separately as a test sentinel.
"""

from __future__ import annotations

from typing import Any

# ----------------------------------------------------------------- MCP tools

# Tools that never touch the network and are always safe in air-gap mode.
# All of these are local file IO (``read_document``, Phenix tools), DB writes
# (``set_status``, ``save_iteration_summary``, knowledge-state mutations),
# or in-container compute (``execute_code``, sandboxed by the kernel network
# namespace per §10.2).
MCP_TOOLS_LOCAL_ONLY: frozenset[str] = frozenset(
    {
        "ping",
        "execute_code",
        "read_document",
        "set_status",
        "set_job_title",
        "save_iteration_summary",
        "set_consensus_answer",
        "update_knowledge_state",
        "add_hypothesis",
        "update_hypothesis",
        "run_phenix_tool",
        "compare_structures",
        "parse_alphafold_confidence",
    }
)

# Tools that hit a configurable external endpoint. In normal deployments the
# endpoint is public (NCBI eutils for PubMed); in air-gap deployments the
# operator must redirect it to a local mirror (RFC §15). When the redirect
# is not configured, the tool is denied — fail-closed.
MCP_TOOLS_NETWORK_DEPENDENT: frozenset[str] = frozenset(
    {
        "search_pubmed",  # PUBMED_BASE_URL → operator-mounted mirror
    }
)

# Union — every tool the MCP server is known to register.
ALL_KNOWN_MCP_TOOLS: frozenset[str] = MCP_TOOLS_LOCAL_ONLY | MCP_TOOLS_NETWORK_DEPENDENT


def allowed_mcp_tools(settings: Any) -> frozenset[str]:
    """Return the set of MCP tool names allowed in this deployment.

    Non-airgap mode: every classified tool is allowed (unclassified tools
    are reported by :func:`unclassified_mcp_tools` — they pass through here
    by default to avoid regressing existing deployments).

    Airgap mode:

    * :data:`MCP_TOOLS_LOCAL_ONLY` always allowed.
    * :data:`MCP_TOOLS_NETWORK_DEPENDENT` allowed **only if** the operator
      configured the redirect. ``search_pubmed`` requires
      ``settings.airgap.pubmed_addr`` to be set.

    The MCP server registration path (or a future runtime gate) reads this
    set to refuse banned tools at startup. A tool whose backend is misrouted
    fails closed: the tool isn't registered, the agent gets a tool-not-found
    error if it tries to call it, the operator sees the misconfig clearly.
    """
    if not _airgap_enabled(settings):
        return ALL_KNOWN_MCP_TOOLS

    allowed = set(MCP_TOOLS_LOCAL_ONLY)
    if getattr(settings.airgap, "pubmed_addr", None):
        allowed.add("search_pubmed")
    return frozenset(allowed)


def unclassified_mcp_tools(registered: set[str]) -> frozenset[str]:
    """Sentinel — return tool names registered by the MCP server but absent
    from :data:`ALL_KNOWN_MCP_TOOLS`.

    The tests use this against a live MCP server registration to catch a
    new tool that was added without an airgap classification; the airgap
    policy default is permissive (the new tool passes), so this is the
    only way to notice the gap before a security-relevant tool sneaks in.
    """
    return frozenset(registered - ALL_KNOWN_MCP_TOOLS)


# ----------------------------------------------------------------- Claude SDK
#
# These are built-in tool names the Claude Code SDK exposes natively (NOT
# via MCP). The AirgapClaudeCodeAgent (PR-2) will pass disallowed_claude
# _builtins(settings) into its ClaudeAgentOptions so the SDK never advertises
# them to the model. The Codex CLI side handles its analogue (web_search)
# at the fork level for non-OpenAI providers — see RFC §10.4.

# Network-capable Claude SDK built-ins. Both fetch external content; either
# would be a direct exfil channel from the agent's reasoning.
CLAUDE_BUILTINS_NETWORK: frozenset[str] = frozenset(
    {
        "WebFetch",
        "WebSearch",
    }
)


def disallowed_claude_builtins(settings: Any) -> frozenset[str]:
    """Return Claude SDK built-in tool names to disable in this deployment.

    Non-airgap: empty (the SDK's default behavior applies).
    Airgap: every :data:`CLAUDE_BUILTINS_NETWORK` entry is disabled.

    PR-2's ``AirgapClaudeCodeAgent`` constructs its ``ClaudeAgentOptions``
    with ``disallowed_tools=disallowed_claude_builtins(settings)``. PR-1's
    factory still refuses ClaudeCompatible providers entirely in airgap
    mode (see ``factory.py``), so the load isn't on this function yet — but
    the declaration is here so PR-2 lands as a one-line wire-up rather
    than a re-derivation of policy.
    """
    if not _airgap_enabled(settings):
        return frozenset()
    return CLAUDE_BUILTINS_NETWORK


# ----------------------------------------------------------------- helpers


def _airgap_enabled(settings: Any) -> bool:
    """Defensive read of ``settings.airgap.enabled``.

    Mirrors the pattern in ``runner.py`` and ``container_manager.py`` so the
    same legacy-SimpleNamespace settings stubs used in non-airgap tests pass
    through cleanly (no ``airgap`` attribute → not enabled).
    """
    airgap = getattr(settings, "airgap", None)
    return bool(getattr(airgap, "enabled", False))


__all__ = (
    "ALL_KNOWN_MCP_TOOLS",
    "CLAUDE_BUILTINS_NETWORK",
    "MCP_TOOLS_LOCAL_ONLY",
    "MCP_TOOLS_NETWORK_DEPENDENT",
    "allowed_mcp_tools",
    "disallowed_claude_builtins",
    "unclassified_mcp_tools",
)
