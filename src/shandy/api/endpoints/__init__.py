"""
API endpoint modules.
"""

from .jobs import router as jobs_router
from .keys import router as keys_router
from .shares import router as shares_router
from .skills import router as skills_router

__all__ = ["jobs_router", "keys_router", "shares_router", "skills_router"]
