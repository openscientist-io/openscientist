"""
GitHub OAuth provider implementation.

Handles GitHub OAuth flow and user profile extraction.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class GitHubProvider:
    """GitHub OAuth provider implementation."""

    @staticmethod
    async def get_user_info(token: dict[str, Any]) -> dict[str, Any]:
        """
        Fetch user profile from GitHub API.

        Args:
            token: OAuth token response containing access_token

        Returns:
            Dictionary with user profile:
            - provider_user_id: GitHub user ID
            - email: Primary email address
            - name: Display name
            - username: GitHub username

        Raises:
            httpx.HTTPError: If API request fails
        """
        access_token = token.get("access_token")
        if not access_token:
            raise ValueError("No access_token in token response")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        async with httpx.AsyncClient() as client:
            # Get user profile
            user_resp = await client.get("https://api.github.com/user", headers=headers)
            user_resp.raise_for_status()
            user_data = user_resp.json()

            # Get primary email if not public
            email = user_data.get("email")
            if not email:
                emails_resp = await client.get(
                    "https://api.github.com/user/emails", headers=headers
                )
                emails_resp.raise_for_status()
                emails = emails_resp.json()
                # Find primary email
                for e in emails:
                    if e.get("primary") and e.get("verified"):
                        email = e["email"]
                        break
                if not email and emails:
                    # Fall back to first verified email
                    email = next((e["email"] for e in emails if e.get("verified")), None)

            if not email:
                logger.warning("No email found for GitHub user %s", user_data.get("login"))
                email = f"{user_data['login']}@users.noreply.github.com"

            return {
                "provider_user_id": str(user_data["id"]),
                "email": email,
                "name": user_data.get("name") or user_data.get("login"),
                "username": user_data.get("login"),
            }
