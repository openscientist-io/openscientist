"""
GitHub OAuth provider implementation.

Handles GitHub OAuth flow and user profile extraction.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _select_primary_verified_email(emails: list[dict[str, Any]]) -> str | None:
    """Return primary verified email from /user/emails records."""
    for record in emails:
        if record.get("primary") and record.get("verified") and record.get("email"):
            return str(record["email"])
    return None


def _select_any_verified_email(emails: list[dict[str, Any]]) -> str | None:
    """Return first verified email from /user/emails records."""
    for record in emails:
        if record.get("verified") and record.get("email"):
            return str(record["email"])
    return None


def _select_profile_email_with_verification(
    user_data: dict[str, Any],
    emails: list[dict[str, Any]],
) -> tuple[str | None, bool]:
    """Select public profile email and infer verification from email records."""
    public_email = user_data.get("email")
    if not public_email:
        return None, False

    email = str(public_email)
    matched = next(
        (record for record in emails if str(record.get("email", "")).lower() == email.lower()),
        None,
    )
    return email, bool(matched and matched.get("verified"))


def _select_fallback_email(emails: list[dict[str, Any]]) -> tuple[str | None, bool]:
    """Select fallback email from /user/emails records."""
    fallback_record: dict[str, Any] | None = None
    for item in emails:
        if item.get("primary") and item.get("email"):
            fallback_record = item
            break

    if fallback_record is None:
        for item in emails:
            if item.get("email"):
                fallback_record = item
                break

    if not fallback_record:
        return None, False
    return str(fallback_record["email"]), bool(fallback_record.get("verified"))


def _resolve_email_and_verification(
    user_data: dict[str, Any],
    emails: list[dict[str, Any]],
) -> tuple[str, bool]:
    """Resolve best available email + verification flag from GitHub profile data."""
    email = _select_primary_verified_email(emails)
    if email:
        return email, True

    email = _select_any_verified_email(emails)
    if email:
        return email, True

    profile_email, profile_verified = _select_profile_email_with_verification(user_data, emails)
    if profile_email:
        return profile_email, profile_verified

    fallback_email, fallback_verified = _select_fallback_email(emails)
    if fallback_email:
        return fallback_email, fallback_verified

    logger.warning("No email found for GitHub user %s", user_data.get("login"))
    return f"{user_data['login']}@users.noreply.github.com", False


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

            email, email_verified = _resolve_email_and_verification(user_data, emails)

            return {
                "provider_user_id": str(user_data["id"]),
                "email": email,
                "name": user_data.get("name") or user_data.get("login"),
                "username": user_data.get("login"),
                "email_verified": email_verified,
            }
