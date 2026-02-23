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
            - email_verified: Whether provider verified the selected email

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

            emails: list[dict[str, Any]] = []
            try:
                emails_resp = await client.get(
                    "https://api.github.com/user/emails", headers=headers
                )
                emails_resp.raise_for_status()
                emails = emails_resp.json()
            except httpx.HTTPError:
                logger.warning(
                    "Could not fetch GitHub email metadata for user %s",
                    user_data.get("login"),
                    exc_info=True,
                )

            email: str | None = None
            email_verified = False

            # Prefer verified emails from the dedicated emails endpoint.
            for record in emails:
                if record.get("primary") and record.get("verified") and record.get("email"):
                    email = str(record["email"])
                    email_verified = True
                    break

            if not email:
                for record in emails:
                    if record.get("verified") and record.get("email"):
                        email = str(record["email"])
                        email_verified = True
                        break

            # Fall back to public profile email when needed.
            if not email:
                public_email = user_data.get("email")
                if public_email:
                    email = str(public_email)
                    matched = next(
                        (
                            record
                            for record in emails
                            if str(record.get("email", "")).lower() == email.lower()
                        ),
                        None,
                    )
                    email_verified = bool(matched and matched.get("verified"))

            # Final fallback: first available email record from /user/emails.
            if not email and emails:
                record = next(
                    (item for item in emails if item.get("primary") and item.get("email")),
                    None,
                ) or next((item for item in emails if item.get("email")), None)
                if record:
                    email = str(record["email"])
                    email_verified = bool(record.get("verified"))

            if not email:
                logger.warning("No email found for GitHub user %s", user_data.get("login"))
                email = f"{user_data['login']}@users.noreply.github.com"
                email_verified = False

            return {
                "provider_user_id": str(user_data["id"]),
                "email": email,
                "name": user_data.get("name") or user_data.get("login"),
                "username": user_data.get("login"),
                "email_verified": email_verified,
            }
