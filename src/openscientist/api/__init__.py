"""
REST API for OpenScientist.

Provides programmatic access to job management and API key management.
All endpoints require API key authentication via Bearer token.

API Key Format:
    Authorization: Bearer <name>:<secret>

Example:
    curl -H "Authorization: Bearer my-key:abc123..." https://openscientist.example.com/api/v1/jobs
"""

from typing import Any

__all__ = ["api_router"]


def __getattr__(name: str) -> Any:
    """Lazily expose package attributes to avoid import-time cycles."""
    if name == "api_router":
        from .router import api_router

        return api_router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
