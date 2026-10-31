"""Agent factory.

``get_agent`` instantiates the configured provider and returns its harness
agent. ``OPENSCIENTIST_HARNESS`` selects it: ``auto`` derives from the provider
family (Claude/Codex), ``omp`` drives any provider, and ``claude_code``/``codex``
require a matching family.
"""

from __future__ import annotations

import logging
from typing import Any

from openscientist.agent.base import AbstractAgent, AgentBackend, AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.agent.omp_agent import OmpAgent
from openscientist.providers import provider_class
from openscientist.providers.base import (
    ClaudeCompatible,
    CodexCompatible,
    OpenAiWireCompatible,
    Provider,
)
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


def _instantiate_provider(provider_id: str) -> Provider:
    """Construct the provider registered under `provider_id` (validates auth)."""
    return provider_class(provider_id)()


def _codex_agent_class() -> type[AbstractAgent[Any]]:
    """Deferred import: ``codex_agent`` imports the optional ``openai_codex`` SDK,
    which is absent in some images (e.g. the web image), so the factory must stay
    importable without it.
    """
    from openscientist.agent.codex_agent import CodexAgent

    return CodexAgent


def agent_class_for_backend(backend: AgentBackend) -> type[AbstractAgent[Any]]:
    """The agent class that implements ``backend``."""
    if backend is AgentBackend.CLAUDE_CODE:
        return ClaudeCodeAgent
    if backend is AgentBackend.OMP:
        return OmpAgent
    return _codex_agent_class()


def _resolve_harness() -> str:
    """The configured harness id (``auto``/``claude_code``/``codex``/``omp``)."""
    return get_settings().provider.harness


def _agent_class_for(cls: type[Provider], harness: str) -> type[AbstractAgent[Any]]:
    """Map a provider class + harness id to its agent class (the one dispatch).

    ``omp`` drives any provider; ``claude_code``/``codex`` require the matching
    family; ``auto`` derives from the family.
    """
    is_claude = issubclass(cls, ClaudeCompatible)
    is_codex = issubclass(cls, CodexCompatible)

    if harness == "omp":
        return OmpAgent
    if harness == "claude_code":
        if not is_claude:
            raise ValueError(
                f"OPENSCIENTIST_HARNESS=claude_code requires a Claude-compatible "
                f"provider, but {cls.__name__} is not one. Use OPENSCIENTIST_HARNESS=omp "
                "to drive this provider, or select an Anthropic-family provider."
            )
        return ClaudeCodeAgent
    if harness == "codex":
        if not is_codex:
            raise ValueError(
                f"OPENSCIENTIST_HARNESS=codex requires an OpenAI-compatible provider, "
                f"but {cls.__name__} is not one. Use OPENSCIENTIST_HARNESS=omp to drive "
                "this provider, or select an OpenAI-family provider."
            )
        return _codex_agent_class()

    # auto: derive from the provider family.
    if is_claude:
        return ClaudeCodeAgent
    if is_codex:
        return _codex_agent_class()
    if issubclass(cls, OpenAiWireCompatible):
        # Speaks the OpenAI wire but is not a Codex backend, so omp is its only
        # harness. Deriving it here keeps the default working rather than making
        # every such provider require an explicit OPENSCIENTIST_HARNESS=omp.
        return OmpAgent
    raise ValueError(
        f"Provider {cls.__name__} does not implement a known agent "
        "compatibility family (ClaudeCompatible or OpenAiWireCompatible)."
    )


def agent_class_for_provider(provider: Provider) -> type[AbstractAgent[Any]]:
    """The agent class that drives a provider instance under the active harness."""
    return _agent_class_for(type(provider), _resolve_harness())


def agent_class_for_provider_id(provider_id: str) -> type[AbstractAgent[Any]]:
    """Agent class for a provider id without instantiating it (UI/prelaunch).

    An unknown id falls back to the Claude agent for labelling.
    """
    try:
        cls = provider_class(provider_id)
    except ValueError:
        return ClaudeCodeAgent
    return _agent_class_for(cls, _resolve_harness())


def build_agent(config: AgentConfig, provider: Provider) -> AbstractAgent[Provider]:
    """Build the agent for an explicit provider (shared by get_agent and chat).

    Reads any per-run model override from ``config.model_override``.
    """
    agent_cls = agent_class_for_provider(provider)
    logger.info("Using %s with provider %s", agent_cls.__name__, provider.id)
    return agent_cls(config, provider)


def get_agent(config: AgentConfig) -> AbstractAgent[Provider]:
    """The agent for the configured provider (provider_id + harness)."""
    return build_agent(config, _instantiate_provider(get_settings().provider.provider_id))
