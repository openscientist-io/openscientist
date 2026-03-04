"""
OAuth configuration and client management.

Provides OAuth client initialization and configuration for supported providers.
"""

import logging

from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]
from starlette.config import Config

from open_scientist.settings import get_settings

logger = logging.getLogger(__name__)

# OAuth configuration
_oauth: OAuth | None = None


def get_oauth_config() -> Config:
    """
    Get Starlette config for OAuth.

    Returns:
        Starlette Config object with OAuth settings
    """
    settings = get_settings()
    return Config(
        environ={
            "GOOGLE_CLIENT_ID": settings.auth.google_client_id or "",
            "GOOGLE_CLIENT_SECRET": settings.auth.google_client_secret or "",
            "GITHUB_CLIENT_ID": settings.auth.github_client_id or "",
            "GITHUB_CLIENT_SECRET": settings.auth.github_client_secret or "",
        }
    )


def get_oauth_client() -> OAuth:
    """
    Get or create the OAuth client singleton.

    Returns:
        OAuth client instance configured with all providers
    """
    global _oauth
    if _oauth is None:
        config = get_oauth_config()
        _oauth = OAuth(config)
        settings = get_settings()

        # Register Google provider
        google_client_id = settings.auth.google_client_id
        if google_client_id:
            _oauth.register(
                name="google",
                client_id=google_client_id,
                client_secret=settings.auth.google_client_secret,
                server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                client_kwargs={"scope": "openid email profile"},
            )
            logger.info("Google OAuth provider registered")
        else:
            logger.info("Google OAuth not configured (GOOGLE_CLIENT_ID not set)")

        # Register GitHub provider
        github_client_id = settings.auth.github_client_id
        if github_client_id:
            _oauth.register(
                name="github",
                client_id=github_client_id,
                client_secret=settings.auth.github_client_secret,
                access_token_url="https://github.com/login/oauth/access_token",
                access_token_params=None,
                authorize_url="https://github.com/login/oauth/authorize",
                authorize_params=None,
                api_base_url="https://api.github.com/",
                client_kwargs={"scope": "user:email"},
            )
            logger.info("GitHub OAuth provider registered")
        else:
            logger.info("GitHub OAuth not configured (GITHUB_CLIENT_ID not set)")

    return _oauth


def is_oauth_configured() -> bool:
    """
    Check if at least one OAuth provider is configured.

    Returns:
        True if GitHub, Google, or mock auth (dev mode) is configured
    """
    settings = get_settings()
    return settings.auth.is_oauth_configured or settings.dev.dev_mode


def is_mock_auth_enabled() -> bool:
    """
    Check if mock authentication is enabled (dev mode only).

    Returns:
        True if OPEN_SCIENTIST_DEV_MODE is enabled
    """
    settings = get_settings()
    return settings.dev.dev_mode
