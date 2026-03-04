"""
Mock OAuth provider for development and testing.

This provider simulates OAuth flow without requiring external OAuth configuration.
Only enabled when SHANDY_DEV_MODE=true.

SECURITY WARNING: Never enable this in production!
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MockProvider:
    """Mock OAuth provider for development and testing."""

    @staticmethod
    async def get_user_info(user_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract user profile from mock OAuth data.

        Args:
            user_data: Mock user data from the login form

        Returns:
            Dictionary with user profile:
            - provider_user_id: Mock user ID
            - email: User's email
            - name: Display name
            - username: Username

        Note:
            This is a simplified version that accepts user data directly
            instead of fetching from an external API.
        """
        email = user_data.get("email", "dev@example.com")
        name = user_data.get("name", "Dev User")
        username = user_data.get("username", email.split("@")[0])

        return {
            "provider_user_id": f"mock_{username}",
            "email": email,
            "name": name,
            "username": username,
        }
