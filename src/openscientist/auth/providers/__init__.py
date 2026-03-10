"""
OAuth provider implementations for OpenScientist.

Each provider module handles the OAuth flow specifics and user info extraction
for a particular authentication provider.
"""

from openscientist.auth.providers.github import GitHubProvider
from openscientist.auth.providers.google import GoogleProvider
from openscientist.auth.providers.mock import MockProvider
from openscientist.auth.providers.orcid import OrcidProvider

__all__ = [
    "GitHubProvider",
    "GoogleProvider",
    "MockProvider",
    "OrcidProvider",
]
