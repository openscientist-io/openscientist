"""
Authentication and authorization for SHANDY.

Provides OAuth-based authentication with GitHub and ORCID providers,
session management, and user context for RLS.
"""

from shandy.auth.middleware import (
    get_current_user_id,
    is_current_user_admin,
    require_admin,
    require_auth,
)
from shandy.auth.oauth import get_oauth_client, is_oauth_configured

__all__ = [
    "require_auth",
    "require_admin",
    "get_oauth_client",
    "is_oauth_configured",
    "get_current_user_id",
    "is_current_user_admin",
]
