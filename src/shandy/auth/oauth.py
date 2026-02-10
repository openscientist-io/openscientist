"""
OAuth configuration and client management.

Provides OAuth client initialization and configuration for supported providers.
"""

import logging
import os
from typing import Optional

from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]
from starlette.config import Config

logger = logging.getLogger(__name__)

# OAuth configuration
_oauth: Optional[OAuth] = None


def get_oauth_config() -> Config:
    """
    Get Starlette config for OAuth.

    Returns:
        Starlette Config object with OAuth settings
    """
    return Config(
        environ={
            "GITHUB_CLIENT_ID": os.getenv("GITHUB_CLIENT_ID", ""),
            "GITHUB_CLIENT_SECRET": os.getenv("GITHUB_CLIENT_SECRET", ""),
            "ORCID_CLIENT_ID": os.getenv("ORCID_CLIENT_ID", ""),
            "ORCID_CLIENT_SECRET": os.getenv("ORCID_CLIENT_SECRET", ""),
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

        # Register GitHub provider
        github_client_id = os.getenv("GITHUB_CLIENT_ID")
        if github_client_id:
            _oauth.register(
                name="github",
                client_id=github_client_id,
                client_secret=os.getenv("GITHUB_CLIENT_SECRET"),
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
        orcid_client_id = os.getenv("ORCID_CLIENT_ID")
        if orcid_client_id:
            # Use production ORCID by default, sandbox for testing
            orcid_base = os.getenv("ORCID_API_BASE", "https://orcid.org")
            _oauth.register(
                name="orcid",
                client_id=orcid_client_id,
                client_secret=os.getenv("ORCID_CLIENT_SECRET"),
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
    return bool(
        os.getenv("GITHUB_CLIENT_ID")
        or os.getenv("ORCID_CLIENT_ID")
        or os.getenv("ENABLE_MOCK_AUTH")
    )


def is_mock_auth_enabled() -> bool:
    """
    Check if mock authentication is enabled (development only).

    Returns:
        True if ENABLE_MOCK_AUTH environment variable is set
    """
    return os.getenv("ENABLE_MOCK_AUTH", "").lower() in ("true", "1", "yes")
