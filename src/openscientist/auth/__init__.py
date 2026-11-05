"""Authentication and authorization for OpenScientist."""

from openscientist.auth.middleware import (
    can_current_user_start_jobs,
    get_current_user_id,
    go_to_login,
    is_current_user_admin,
    require_admin,
    require_auth,
    session_still_valid_for,
)
from openscientist.auth.oauth import get_oauth_client, is_oauth_configured

__all__ = [
    "can_current_user_start_jobs",
    "get_current_user_id",
    "get_oauth_client",
    "go_to_login",
    "is_current_user_admin",
    "is_oauth_configured",
    "require_admin",
    "require_auth",
    "session_still_valid_for",
]
