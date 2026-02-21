"""
OAuth provider implementations for SHANDY.

Each provider module handles the OAuth flow specifics and user info extraction
for a particular authentication provider.
"""

from shandy.auth.providers.github import GitHubProvider
from shandy.auth.providers.google import GoogleProvider
from shandy.auth.providers.mock import MockProvider
from shandy.auth.providers.orcid import ORCIDProvider

__all__ = [
    "GitHubProvider",
    "GoogleProvider",
    "MockProvider",
    "ORCIDProvider",
]
