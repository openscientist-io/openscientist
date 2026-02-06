"""
Provider abstraction for model access (Anthropic, CBORG, Vertex AI, Bedrock).

Providers handle:
- Environment configuration for Claude CLI
- Cost tracking and budget enforcement
- Provider-specific authentication and setup
"""

import os

from shandy.providers.base import BaseProvider, CostInfo


def get_provider() -> BaseProvider:
    """
    Get the configured provider based on environment.

    Returns:
        Provider instance (CborgProvider, VertexProvider, or BedrockProvider)

    Raises:
        ValueError: If provider is unknown or misconfigured

    Environment:
        CLAUDE_PROVIDER: Provider name ("cborg", "vertex", "bedrock")
                        Defaults to "cborg" if not set
    """
    provider_name = os.getenv("CLAUDE_PROVIDER", "cborg").lower()

    if provider_name == "anthropic":
        from shandy.providers.cborg import AnthropicProvider
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
    else:
        raise ValueError(
            f"Unknown provider '{provider_name}'. "
            f"Valid options: anthropic, cborg, vertex, bedrock"
        )


__all__ = ["get_provider", "BaseProvider", "CostInfo"]
