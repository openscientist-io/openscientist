"""
Agent factory for OpenScientist.

`get_agent(config)` instantiates the configured provider and returns the
agent that drives its compatibility family: `ClaudeCompatible` providers
get a `ClaudeCodeAgent`, `CodexCompatible` providers a `CodexAgent`.
"""

from __future__ import annotations

import logging
from typing import Any

from openscientist.agent.base import AbstractAgent, AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.providers.anthropic import AnthropicProvider
from openscientist.providers.azure_openai import AzureOpenAIProvider
from openscientist.providers.base import ClaudeCompatible, CodexCompatible, Provider
from openscientist.providers.bedrock import BedrockProvider
from openscientist.providers.cborg import CborgProvider
from openscientist.providers.foundry import FoundryProvider
from openscientist.providers.ollama import OllamaProvider
from openscientist.providers.openai import OpenAIDirectProvider
from openscientist.providers.vertex import VertexProvider
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)

_PROVIDER_REGISTRY: dict[str, type[Provider]] = {
    "anthropic": AnthropicProvider,
    "cborg": CborgProvider,
    "vertex": VertexProvider,
    "bedrock": BedrockProvider,
    "foundry": FoundryProvider,
    "openai": OpenAIDirectProvider,
    "azure-openai": AzureOpenAIProvider,
    "ollama": OllamaProvider,
}


def _instantiate_provider(provider_id: str) -> Provider:
    """Construct the provider registered under `provider_id`."""
    cls = _PROVIDER_REGISTRY.get(provider_id.lower())
    if cls is None:
        valid = ", ".join(sorted(_PROVIDER_REGISTRY))
        raise ValueError(f"Unknown provider {provider_id!r}. Valid options: {valid}")
    return cls()


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

    The active provider is selected by `settings.provider.provider_id`. The
    agent class is chosen by the provider's compatibility family. When
    `settings.airgap.enabled` is True, the air-gap variant of the family's
    agent is selected (currently only `AirgapCodexAgent`; the Claude SDK
    built-in gating from RFC §10.3 lands in a follow-up PR).
    """
    settings = get_settings()
    provider = _instantiate_provider(settings.provider.provider_id)
    # ClaudeCompatible is checked first: a hypothetical multi-family provider
    # prefers the mature Claude path until a real hybrid case appears.
    if isinstance(provider, ClaudeCompatible):
        if settings.airgap.enabled:
            raise ValueError(
                "Air-gap mode is enabled but the active provider "
                f"({provider.id}) is Claude-compatible. PR-1 ships only the "
                "AirgapCodexAgent; the AirgapClaudeCodeAgent (with SDK "
                "built-in tool gating per RFC §10.3) lands in a follow-up "
                "PR. Switch to a Codex-compatible provider — `ollama` "
                "(local, gpt-oss-120b is the RFC §7.4 reference), or "
                "`openai` / `azure-openai` if you have a cloud-compatible "
                "internal endpoint — or disable OPENSCIENTIST_AIR_GAPPED."
            )
        logger.info("Using ClaudeCodeAgent with provider %s", provider.id)
        return ClaudeCodeAgent(config, provider, model_override=config.model_override)
    if isinstance(provider, CodexCompatible):
        # Deferred import: the codex SDK is only needed on the codex path, so
        # environments without it (e.g. images that ship only the Claude SDK)
        # can still import the factory.
        from openscientist.agent.codex_agent import CodexAgent

        if settings.airgap.enabled:
            from openscientist.airgap.codex_agent import AirgapCodexAgent
            from openscientist.airgap.egress_registry import (
                validate_provider_for_airgap,
            )

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
        logger.info("Using CodexAgent with provider %s", provider.id)
        return CodexAgent(config, provider)
    raise ValueError(
        f"Provider {type(provider).__name__} does not implement a known agent "
        "compatibility family (ClaudeCompatible or CodexCompatible)."
    )
