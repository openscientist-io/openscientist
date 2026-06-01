"""
Agent factory for OpenScientist.

`get_agent(config)` instantiates the configured provider and returns the
agent that drives its compatibility family: `ClaudeCompatible` providers
get a `ClaudeCodeAgent`, `CodexCompatible` providers a `CodexAgent`.
"""

from __future__ import annotations

import logging

from openscientist.agent.base import AbstractAgent, AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.providers.anthropic import AnthropicProvider
from openscientist.providers.azure_openai import AzureOpenAIProvider
from openscientist.providers.base import ClaudeCompatible, CodexCompatible, Provider
from openscientist.providers.bedrock import BedrockProvider
from openscientist.providers.bedrock_openai import BedrockOpenAIProvider
from openscientist.providers.cborg import CborgProvider
from openscientist.providers.foundry import FoundryProvider
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
    "bedrock-openai": BedrockOpenAIProvider,
}


def _instantiate_provider(provider_id: str) -> Provider:
    """Construct the provider registered under `provider_id`."""
    cls = _PROVIDER_REGISTRY.get(provider_id.lower())
    if cls is None:
        valid = ", ".join(sorted(_PROVIDER_REGISTRY))
        raise ValueError(f"Unknown provider {provider_id!r}. Valid options: {valid}")
    return cls()


def get_agent(config: AgentConfig) -> AbstractAgent[Provider]:
    """Return the agent for the configured provider.

    The active provider is selected by `settings.provider.provider_id`. The
    agent class is chosen by the provider's compatibility family.
    """
    provider = _instantiate_provider(get_settings().provider.provider_id)
    # ClaudeCompatible is checked first: a hypothetical multi-family provider
    # prefers the mature Claude path until a real hybrid case appears.
    if isinstance(provider, ClaudeCompatible):
        logger.info("Using ClaudeCodeAgent with provider %s", provider.id)
        return ClaudeCodeAgent(config, provider)
    if isinstance(provider, CodexCompatible):
        # Deferred import: the codex SDK is only needed on the codex path, so
        # environments without it (e.g. images that ship only the Claude SDK)
        # can still import the factory.
        from openscientist.agent.codex_agent import CodexAgent

        logger.info("Using CodexAgent with provider %s", provider.id)
        return CodexAgent(config, provider)
    raise ValueError(
        f"Provider {type(provider).__name__} does not implement a known agent "
        "compatibility family (ClaudeCompatible or CodexCompatible)."
    )
