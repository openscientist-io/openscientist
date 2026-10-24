"""Shared helpers for Anthropic-compatible providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openscientist.settings import ProviderSettings


def anthropic_container_env(provider: ProviderSettings) -> dict[str, str]:
    """The ANTHROPIC_* auth env shared by the anthropic and cborg providers."""
    pairs = [
        ("ANTHROPIC_API_KEY", provider.anthropic_api_key),
        ("ANTHROPIC_AUTH_TOKEN", provider.anthropic_auth_token),
        ("CLAUDE_CODE_OAUTH_TOKEN", provider.claude_code_oauth_token),
        ("ANTHROPIC_BASE_URL", provider.anthropic_base_url),
    ]
    return {key: value for key, value in pairs if value}
