"""
ORCID OAuth provider implementation.

Handles ORCID OAuth/OIDC flow and user profile extraction.
"""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _extract_verified_email(email_data: dict[str, Any]) -> str | None:
    """Extract a verified email from ORCID email API response."""
    for record in email_data.get("email", []):
        if record.get("verified") and record.get("email"):
            return str(record["email"])
    return None


class OrcidProvider:
    """ORCID OAuth provider implementation."""

    @staticmethod
    async def get_user_info(token: dict[str, Any]) -> dict[str, Any]:
        """
        Extract user profile from ORCID token response.

        ORCID's token response includes the ORCID iD and name directly.
        Email is resolved via OIDC claims, the public email API, or a
        fallback to ``{orcid}@orcid.invalid``.

        Args:
            token: OAuth token response containing access_token, orcid, name

        Returns:
            Dictionary with user profile:
            - provider_user_id: ORCID iD (e.g. "0000-0002-1234-5678")
            - email: Email address
            - name: Display name
            - email_verified: Whether the email was verified

        Raises:
            ValueError: If no access_token in token response
        """
        access_token = token.get("access_token")
        if not access_token:
            raise ValueError("No access_token in token response")

        orcid_id: str = token.get("orcid", "")
        name: str = token.get("name") or ""

        # If name is empty, try OIDC userinfo
        userinfo = token.get("userinfo") or {}
        if not name:
            given = userinfo.get("given_name", "")
            family = userinfo.get("family_name", "")
            name = f"{given} {family}".strip()

        # Fall back to ORCID iD string if name is still empty
        if not name:
            name = orcid_id or "Unknown"

        # Resolve email with ordered fallback
        email: str | None = None
        email_verified = False

        # 1. Check OIDC userinfo claims
        oidc_email = userinfo.get("email")
        if oidc_email:
            email = str(oidc_email)
            email_verified = True

        # 2. Try ORCID Member API (requires /read-limited scope)
        if not email and orcid_id:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"https://api.orcid.org/v3.0/{orcid_id}/email",
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {access_token}",
                        },
                    )
                    resp.raise_for_status()
                    verified_email = _extract_verified_email(resp.json())
                    if verified_email:
                        email = verified_email
                        email_verified = True
            except httpx.HTTPError:
                logger.warning(
                    "Could not fetch email from ORCID API for %s",
                    orcid_id,
                    exc_info=True,
                )

        # 3. Fall back to synthetic address
        if not email:
            email = f"{orcid_id}@orcid.invalid" if orcid_id else "unknown@orcid.invalid"
            email_verified = False

        return {
            "provider_user_id": orcid_id,
            "email": email,
            "name": name,
            "email_verified": email_verified,
        }
