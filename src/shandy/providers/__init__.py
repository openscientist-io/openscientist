"""
Provider abstraction for model access (Anthropic, CBORG, Vertex AI, Bedrock, Codex).

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
        BedrockProvider, or CodexProvider)

    Raises:
        ValueError: If provider is unknown or misconfigured

    Environment:
        CLAUDE_PROVIDER: Provider name ("anthropic", "cborg", "vertex", "bedrock", "codex")
                        Defaults to "cborg" if not set
    """
    settings = get_settings()
    provider_name = settings.provider.claude_provider.lower()

    if provider_name == "anthropic":
        from shandy.providers.anthropic import AnthropicProvider

        return AnthropicProvider()
    elif provider_name == "cborg":
        from shandy.providers.cborg import CborgProvider

        return CborgProvider()
    elif provider_name == "vertex":
        from shandy.providers.vertex import VertexProvider

        return VertexProvider()
    elif provider_name == "bedrock":
        from shandy.providers.bedrock import BedrockProvider

        return BedrockProvider()
    elif provider_name == "codex":
        from shandy.providers.codex import CodexProvider

        return CodexProvider()
    else:
        raise ValueError(
            f"Unknown provider '{provider_name}'. Valid options: anthropic, cborg, vertex, bedrock, codex"
        )


__all__ = ["get_provider", "BaseProvider", "CostInfo"]
