"""
Agent factory for OpenScientist.

`get_agent(config)` instantiates the configured provider and returns the
agent that drives its compatibility family: `ClaudeCompatible` providers
get a `ClaudeCodeAgent`, `CodexCompatible` providers a `CodexAgent`.
"""

from __future__ import annotations

import logging
from typing import Any

from openscientist.agent.base import AbstractAgent, AgentBackend, AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.providers import provider_class
from openscientist.providers.base import ClaudeCompatible, CodexCompatible, Provider
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


def _instantiate_provider(provider_id: str) -> Provider:
    """Construct the provider registered under `provider_id` (validates auth)."""
    return provider_class(provider_id)()


def _agent_class_for_provider_class(cls: type[Provider]) -> type[AbstractAgent[Any]]:
    """The one provider-family -> agent-class dispatch.

    Every other resolver derives from this, so adding a new agent family is a
    single edit here. ClaudeCompatible is preferred when a provider somehow
    implements both families (hypothetical, no real provider does).
    """
    if issubclass(cls, ClaudeCompatible):
        return ClaudeCodeAgent
    if issubclass(cls, CodexCompatible):
        # Deferred import: the codex SDK is only needed on the codex path, so
        # environments without it (e.g. images shipping only the Claude SDK)
        # can still import the factory.
        from openscientist.agent.codex_agent import CodexAgent

        return CodexAgent
    raise ValueError(
        f"Provider {cls.__name__} does not implement a known agent "
        "compatibility family (ClaudeCompatible or CodexCompatible)."
    )


def agent_class_for_provider(provider: Provider) -> type[AbstractAgent[Any]]:
    """The agent class that drives a provider instance."""
    return _agent_class_for_provider_class(type(provider))


def agent_class_for_provider_id(provider_id: str) -> type[AbstractAgent[Any]]:
    """The agent class for a provider id without instantiating anything.

    Lets the web/orchestrator process (no agent instance) reach a backend's
    classmethods, e.g. ``provision_host_prelaunch`` before the agent container
    launches. An unknown id falls back to the Claude agent (UI labelling).
    """
    try:
        cls = provider_class(provider_id)
    except ValueError:
        return ClaudeCodeAgent
    return _agent_class_for_provider_class(cls)


def backend_for_provider_id(provider_id: str) -> AgentBackend:
    """The agent backend for a provider id without instantiating it (UI)."""
    return agent_class_for_provider_id(provider_id).backend


def build_agent(config: AgentConfig, provider: Provider) -> AbstractAgent[Provider]:
    """Construct the agent that drives an explicit provider instance.

    Shared by `get_agent` (provider resolved from settings) and the chat path
    (which already holds a provider and needs a single build). The agent reads
    any per-run model override from `config.model_override`.
    """
    agent_cls = agent_class_for_provider(provider)
    logger.info("Using %s with provider %s", agent_cls.__name__, provider.id)
    return agent_cls(config, provider)


_DEFAULT_AIRGAP_PORT = 443


def _parse_airgap_addr(addr: str) -> tuple[str, int] | None:
    """Parse ``host[:port]`` allowing IPv6 in bracket form.

    Codex Review-7 BUG #3 (fixed): the prior ``rpartition(':')`` split
    treated the last colon as the port separator, so:

    * IPv6 literals (e.g. ``[::1]:8443``, ``[fe80::1]:443``) were chopped at
      the wrong colon, producing a host like ``[fe80`` and a port like
      ``:1]:443`` that fails ``isdigit()`` — silently dropped from the
      allowlist.
    * Bare ``host`` with no port was rejected (``rpartition`` returns
      ``('', '', 'host')`` for a string with no colon), also silently
      dropped.

    Now: a missing port defaults to :data:`_DEFAULT_AIRGAP_PORT` (443),
    bracketed IPv6 hosts are unwrapped, and we use
    :func:`urllib.parse.urlsplit` on a synthesized ``scheme://`` URL so
    Python's URL parser does the bracket-aware tokenization for us.

    Returns ``None`` for inputs we can't parse rather than raising — the
    caller drops them from the allowlist, and the operator sees the gap
    when the subsequent provider-target validation fails.
    """
    from urllib.parse import urlsplit

    s = addr.strip()
    if not s:
        return None
    # urlsplit handles bracketed IPv6, port-less hosts, and rejects garbage.
    try:
        parts = urlsplit(f"airgap://{s}")
    except ValueError:
        return None
    host = parts.hostname  # None if unparseable; brackets already stripped
    if not host:
        return None
    try:
        port = parts.port  # raises ValueError if non-numeric, else int|None
    except ValueError:
        return None
    return host, port if port is not None else _DEFAULT_AIRGAP_PORT


def _airgap_allowlist_from_settings(settings: Any) -> set[tuple[str, int]]:
    """Build the egress allowlist from ``settings.airgap.llm_addr`` (and
    ``pubmed_addr`` for completeness).

    The LLM endpoint is the load-bearing one for provider validation; the
    PubMed mirror is included because the same allowlist gets recorded
    into the per-job attestation (§14).
    """
    allowlist: set[tuple[str, int]] = set()
    for addr in (settings.airgap.llm_addr, settings.airgap.pubmed_addr):
        if not addr:
            continue
        parsed = _parse_airgap_addr(addr)
        if parsed is None:
            continue
        allowlist.add(parsed)
    return allowlist


def get_agent(config: AgentConfig) -> AbstractAgent[Provider]:
    """Return the agent for the configured provider.

    The active provider is selected by `settings.provider.provider_id`, and its
    compatibility family chooses the agent class via :func:`build_agent`.
    When `settings.airgap.enabled` is True, the air-gap variant is selected
    instead and the provider's egress targets are validated against the
    operator's allowlist before the agent is instantiated (currently only
    `AirgapCodexAgent`; the Claude SDK built-in gating from RFC §10.3 lands
    in a follow-up PR).
    """
    settings = get_settings()
    provider = _instantiate_provider(settings.provider.provider_id)
    if not settings.airgap.enabled:
        return build_agent(config, provider)

    # Airgap dispatch. ClaudeCompatible providers aren't supported in PR-1
    # because we ship only the AirgapCodexAgent — the AirgapClaudeCodeAgent
    # (with SDK built-in tool gating per RFC §10.3) lands in a follow-up.
    # Exception: the managed-LLM egress path (RFC §7.5 Pattern A, e.g.
    # Bedrock under AWS BAA for HIPAA use cases) opt-in lets a
    # ClaudeCompatible provider run against the regular ClaudeCodeAgent
    # with the kernel-level allowlist as the only enforcement boundary.
    # SDK built-ins are ungated; this is a documented gap, with VPC
    # endpoint (Pattern B) as the migration target.
    if isinstance(provider, ClaudeCompatible):
        if not settings.airgap.allow_managed_llm_egress:
            raise ValueError(
                "Air-gap mode is enabled but the active provider "
                f"({provider.id}) is Claude-compatible. PR-1 ships only the "
                "AirgapCodexAgent; the AirgapClaudeCodeAgent (with SDK "
                "built-in tool gating per RFC §10.3) lands in a follow-up "
                "PR. Switch to a Codex-compatible provider — `ollama` "
                "(local, gpt-oss-120b is the RFC §7.4 reference), or "
                "`openai` / `azure-openai` if you have a cloud-compatible "
                "internal endpoint — or disable OPENSCIENTIST_AIR_GAPPED. "
                "For the managed-LLM egress use case (e.g. Bedrock under "
                "AWS BAA), set "
                "OPENSCIENTIST_AIRGAP_ALLOW_MANAGED_LLM_EGRESS=true; see "
                "RFC §7.5."
            )

        # Pattern A path. Validate the provider's egress targets against
        # the operator's allowlist (same gate as the Codex path), then
        # return the regular ClaudeCodeAgent and log a loud warning so
        # the operator sees the reduced-isolation posture in every job's
        # logs.
        from openscientist.airgap.egress_registry import (
            validate_provider_for_airgap,
        )

        allowlist = _airgap_allowlist_from_settings(settings)
        targets = validate_provider_for_airgap(provider.id, settings, allowlist)
        logger.warning(
            "AIRGAP: managed-LLM egress enabled for provider %s → %s. "
            "Reduced isolation: SDK built-ins are ungated and traffic "
            "exits the per-job network to a cloud endpoint. "
            "HIPAA-eligible under the cloud provider's BAA (e.g. AWS BAA "
            "for Bedrock) but weaker than RFC Pattern B (VPC endpoint). "
            "See RFC §7.5.",
            provider.id,
            sorted(targets),
        )
        return build_agent(config, provider)
    if not isinstance(provider, CodexCompatible):
        raise ValueError(
            f"Provider {type(provider).__name__} does not implement a known "
            "agent compatibility family (ClaudeCompatible or CodexCompatible)."
        )

    from openscientist.airgap.codex_agent import AirgapCodexAgent
    from openscientist.airgap.egress_registry import validate_provider_for_airgap

    # Codex Review-6 BUG (fixed): validate the provider's egress
    # targets against the operator's allowlist BEFORE instantiating
    # the airgap agent. Without this, an unsupported provider
    # (Bedrock SDK regional client, OpenAI default endpoint, etc.)
    # would silently get wrapped in AirgapCodexAgent and proceed to
    # whatever endpoint it computes — defeating the §7 allowlist.
    allowlist = _airgap_allowlist_from_settings(settings)
    targets = validate_provider_for_airgap(provider.id, settings, allowlist)
    logger.info(
        "Air-gap egress validated for provider %s → %s",
        provider.id,
        sorted(targets),
    )
    logger.info("Using AirgapCodexAgent (air-gap mode) with provider %s", provider.id)
    return AirgapCodexAgent(config, provider)
