"""
OAuth configuration and client management.

Provides OAuth client initialization and configuration for supported providers.
"""

import logging
from typing import Optional

from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]
from starlette.config import Config

from shandy.settings import get_settings

logger = logging.getLogger(__name__)

# OAuth configuration
_oauth: Optional[OAuth] = None


def get_oauth_config() -> Config:
    """
    Get Starlette config for OAuth.

    Returns:
        Starlette Config object with OAuth settings
    """
    settings = get_settings()
    return Config(
        environ={
            "GITHUB_CLIENT_ID": settings.auth.github_client_id or "",
            "GITHUB_CLIENT_SECRET": settings.auth.github_client_secret or "",
            "ORCID_CLIENT_ID": settings.auth.orcid_client_id or "",
            "ORCID_CLIENT_SECRET": settings.auth.orcid_client_secret or "",
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

        # Register ORCID provider
        orcid_client_id = settings.auth.orcid_client_id
        if orcid_client_id:
            # Use production ORCID by default, sandbox for testing
            orcid_base = settings.auth.orcid_api_base
            _oauth.register(
                name="orcid",
                client_id=orcid_client_id,
                client_secret=settings.auth.orcid_client_secret,
                access_token_url=f"{orcid_base}/oauth/token",
                access_token_params=None,
                authorize_url=f"{orcid_base}/oauth/authorize",
                authorize_params=None,
                api_base_url=f"{orcid_base}/",
                client_kwargs={"scope": "/authenticate"},
            )
            logger.info("ORCID OAuth provider registered")
        else:
            logger.info("ORCID OAuth not configured (ORCID_CLIENT_ID not set)")

    return _oauth


def is_oauth_configured() -> bool:
    """
    Check if at least one OAuth provider is configured.

    Returns:
        True if GitHub, ORCID, or Mock OAuth is configured
    """
    settings = get_settings()
    return settings.auth.is_oauth_configured


def is_mock_auth_enabled() -> bool:
    """
    Check if mock authentication is enabled (development only).

    Returns:
        True if ENABLE_MOCK_AUTH environment variable is set
    """
    settings = get_settings()
    return settings.auth.enable_mock_auth
