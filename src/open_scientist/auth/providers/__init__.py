"""
OAuth provider implementations for OpenScientist.

Each provider module handles the OAuth flow specifics and user info extraction
for a particular authentication provider.
"""

from open_scientist.auth.providers.github import GitHubProvider
from open_scientist.auth.providers.google import GoogleProvider
from open_scientist.auth.providers.mock import MockProvider

__all__ = [
    "GitHubProvider",
    "GoogleProvider",
    "MockProvider",
]
