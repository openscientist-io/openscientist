"""
Google OAuth provider implementation.

Handles Google OAuth flow and user profile extraction.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GoogleProvider:
    """Google OAuth provider implementation."""

    @staticmethod
    async def get_user_info(token: dict[str, Any]) -> dict[str, Any]:
        """
        Fetch user profile from Google userinfo API.

        Args:
            token: OAuth token response containing access_token

        Returns:
            Dictionary with user profile:
            - provider_user_id: Google user ID (sub)
            - email: Email address
            - name: Display name

        Raises:
            httpx.HTTPError: If API request fails
        """
        access_token = token.get("access_token")
        if not access_token:
            raise ValueError("No access_token in token response")

        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()

            email = data.get("email")
            if not email:
                logger.warning("No email found for Google user %s", data.get("sub"))
                email = f"{data['sub']}@google.invalid"

            return {
                "provider_user_id": data["sub"],
                "email": email,
                "name": data.get("name") or email,
            }
