"""
Provider abstraction for model access (Anthropic, CBORG, Vertex AI, Bedrock, Codex, Azure Foundry).

Providers handle:
- Environment configuration for Claude CLI
- Cost tracking and budget enforcement
- Provider-specific authentication and setup
"""

from shandy.providers.base import BaseProvider, CostInfo
from shandy.settings import get_settings


def get_provider() -> BaseProvider:
    """
    Get the configured provider based on environment.

    Returns:
        Provider instance (AnthropicProvider, CborgProvider, VertexProvider,
        BedrockProvider, CodexProvider, or FoundryProvider)

    Raises:
        ValueError: If provider is unknown or misconfigured

    Environment:
        CLAUDE_PROVIDER: Provider name ("anthropic", "cborg", "vertex", "bedrock", "codex", "foundry")
                        Defaults to "anthropic" if not set
    """
    settings = get_settings()
    provider_name = settings.provider.claude_provider.lower()

    if provider_name == "anthropic":
        from shandy.providers.anthropic import AnthropicProvider

        return AnthropicProvider()
    if provider_name == "cborg":
        from shandy.providers.cborg import CborgProvider

        return CborgProvider()
    if provider_name == "vertex":
        from shandy.providers.vertex import VertexProvider

        return VertexProvider()
    if provider_name == "bedrock":
        from shandy.providers.bedrock import BedrockProvider

        return BedrockProvider()
    if provider_name == "codex":
        from shandy.providers.codex import CodexProvider

        return CodexProvider()
    if provider_name == "foundry":
        from shandy.providers.foundry import FoundryProvider

        return FoundryProvider()
    raise ValueError(
        f"Unknown provider '{provider_name}'. Valid options: anthropic, cborg, vertex, bedrock, codex, foundry"
    )


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
    import os

    # Testing hook: simulate provider misconfiguration for E2E tests
    if os.environ.get("SIMULATE_PROVIDER_ERROR") == "true":  # env-ok
        return (
            False,
            "anthropic",
            [
                "ANTHROPIC_API_KEY is missing or invalid",
                "Please contact your administrator to configure API credentials",
            ],
        )

    settings = get_settings()
    provider_name = settings.provider.claude_provider.lower()

    valid_providers = ("anthropic", "cborg", "vertex", "bedrock", "codex", "foundry")
    if provider_name not in valid_providers:
        return (
            False,
            provider_name,
            [f"Unknown provider '{provider_name}'. Valid options: {', '.join(valid_providers)}"],
        )

    try:
        get_provider()
        return (True, provider_name, [])
    except ValueError as e:
        # Extract error messages from the exception
        error_str = str(e)
        errors = [line.strip() for line in error_str.split("\n") if line.strip()]
        return (False, provider_name, errors)


__all__ = ["get_provider", "check_provider_config", "BaseProvider", "CostInfo"]
