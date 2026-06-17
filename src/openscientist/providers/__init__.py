"""
Provider abstraction for model access (Anthropic, CBORG, Vertex AI, Bedrock, Azure Foundry).

Providers handle:
- Environment configuration for Claude CLI
- Cost tracking and budget enforcement
- Provider-specific authentication and setup
"""

import importlib
from typing import cast

from openscientist.providers.base import CostInfo, Provider
from openscientist.settings import get_settings

# The single provider registry: id -> (module, class name). Dotted paths keep
# provider SDK dependencies optional (imported on demand) and avoid importing
# the agent layer here. The agent factory derives from this, so there is no
# second registry to keep in sync.
_PROVIDER_CLASS_PATHS: dict[str, tuple[str, str]] = {
    "anthropic": ("openscientist.providers.anthropic", "AnthropicProvider"),
    "cborg": ("openscientist.providers.cborg", "CborgProvider"),
    "vertex": ("openscientist.providers.vertex", "VertexProvider"),
    "bedrock": ("openscientist.providers.bedrock", "BedrockProvider"),
    "foundry": ("openscientist.providers.foundry", "FoundryProvider"),
    "openai": ("openscientist.providers.openai", "OpenAIDirectProvider"),
    "azure-openai": ("openscientist.providers.azure_openai", "AzureOpenAIProvider"),
    "ollama": ("openscientist.providers.ollama", "OllamaProvider"),
}


def provider_ids() -> tuple[str, ...]:
    """The registered provider ids (the single source of truth)."""
    return tuple(_PROVIDER_CLASS_PATHS)


def provider_class(provider_name: str) -> type[Provider]:
    """Resolve a provider id to its class without instantiating it.

    Instantiation validates auth (``Provider.__init__``), which a caller that
    only needs to inspect the class (e.g. to derive the agent backend for a
    past job) may not have configured. The class is imported on demand so
    provider SDK dependencies stay optional.
    """
    try:
        module_path, class_name = _PROVIDER_CLASS_PATHS[provider_name.lower()]
    except KeyError:
        raise ValueError(
            f"Unknown provider {provider_name!r}. Valid options: {', '.join(provider_ids())}"
        ) from None
    module = importlib.import_module(module_path)
    return cast("type[Provider]", getattr(module, class_name))


def get_provider() -> Provider:
    """
    Get the configured provider based on environment.

    Returns:
        Provider instance. Anthropic/CBORG/Vertex/Bedrock/Foundry are
        ClaudeCompatible, OpenAI/Azure-OpenAI/Ollama are CodexCompatible.

    Raises:
        ValueError: If provider is unknown or misconfigured

    Environment:
        OPENSCIENTIST_PROVIDER: Provider name ("anthropic", "cborg", "vertex",
                               "bedrock", "foundry", "openai", "azure-openai",
                               "ollama"). Required: an unset value raises at
                               startup.
    """
    return provider_class(get_settings().provider.provider_id)()


def check_provider_config() -> tuple[bool, str, list[str]]:
    """
    Check if the provider is properly configured without raising exceptions.

    Returns:
        Tuple of (is_configured, provider_name, error_messages)
        - is_configured: True if provider can be instantiated
        - provider_name: Name of the configured provider
        - error_messages: List of configuration error messages (empty if configured)

    Environment:
        SIMULATE_PROVIDER_ERROR: Set to "true" for E2E testing of error UI
    """
    settings = get_settings()

    # Testing hook: simulate provider misconfiguration for E2E tests
    if settings.dev.simulate_provider_error:
        return (
            False,
            "anthropic",
            [
                "ANTHROPIC_API_KEY is missing or invalid",
                "Please contact your administrator to configure API credentials",
            ],
        )

    provider_name = settings.provider.provider_id.lower()

    if provider_name not in _PROVIDER_CLASS_PATHS:
        return (
            False,
            provider_name,
            [f"Unknown provider '{provider_name}'. Valid options: {', '.join(provider_ids())}"],
        )

    try:
        get_provider()
        return (True, provider_name, [])
    except ValueError as e:
        # Extract error messages from the exception
        error_str = str(e)
        errors = [line.strip() for line in error_str.split("\n") if line.strip()]
        return (False, provider_name, errors)


__all__ = [
    "CostInfo",
    "check_provider_config",
    "get_provider",
    "provider_class",
    "provider_ids",
]
