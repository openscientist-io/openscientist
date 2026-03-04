"""
Authentication and authorization for Open Scientist.

Provides OAuth-based authentication with GitHub and Google providers,
session management, and user context for RLS.
"""

from open_scientist.auth.middleware import (
    can_current_user_start_jobs,
    get_current_user_id,
    is_current_user_admin,
    is_current_user_approved,
    require_admin,
    require_auth,
)
from open_scientist.auth.oauth import get_oauth_client, is_oauth_configured

__all__ = [
    "can_current_user_start_jobs",
    "get_current_user_id",
    "get_oauth_client",
    "is_current_user_admin",
    "is_current_user_approved",
    "is_oauth_configured",
    "require_admin",
    "require_auth",
]
