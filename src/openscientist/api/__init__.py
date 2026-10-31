"""REST API for OpenScientist.

All endpoints require Bearer authentication with a ``name:secret`` API key.
"""

from typing import Any

__all__ = ["api_router"]


def __getattr__(name: str) -> Any:
    """Lazily expose package attributes to avoid import-time cycles."""
    if name == "api_router":
        from .router import api_router

        return api_router
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
