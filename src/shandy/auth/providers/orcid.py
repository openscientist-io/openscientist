"""
ORCID OAuth provider implementation.

Handles ORCID OAuth flow and user profile extraction.

ORCID's token response with the /authenticate scope includes the user's
name and ORCID iD directly, so no separate API call is needed for basic
authentication. The record API (at pub.orcid.org) requires /read-limited
scope and is only used as an optional enhancement to fetch the user's email.
"""

import logging
from typing import Any

import httpx

from shandy.settings import get_settings

logger = logging.getLogger(__name__)


class ORCIDProvider:
    """ORCID OAuth provider implementation."""

    @staticmethod
    async def get_user_info(token: dict[str, Any]) -> dict[str, Any]:
        """
        Extract user profile from ORCID token response.

        ORCID includes 'name' and 'orcid' fields directly in the OAuth token
        response, so basic authentication doesn't require an API call.

        Optionally attempts to fetch the user's email from the ORCID record
        API (requires the email to be set as public in the user's profile).

        Args:
            token: OAuth token response containing access_token, orcid, and name

        Returns:
            Dictionary with user profile:
            - provider_user_id: ORCID iD
            - email: Email address (synthetic fallback if not public)
            - name: Display name
        """
        access_token = token.get("access_token")
        orcid_id = token.get("orcid")

        if not access_token or not orcid_id:
            raise ValueError("Missing access_token or orcid in token response")

        # ORCID token response includes the user's name directly
        name = token.get("name", "").strip() or orcid_id

        # Try to fetch email from the ORCID record API (pub.orcid.org).
        # This only works if the user has made their email public.
        email = await ORCIDProvider._try_fetch_email(access_token, orcid_id)

        if not email:
            logger.info("No public email for ORCID user %s, using synthetic", orcid_id)
            email = f"{orcid_id}@orcid.org"

        return {
            "provider_user_id": orcid_id,
            "email": email,
            "name": name,
            "orcid": orcid_id,
        }

    @staticmethod
    async def _try_fetch_email(access_token: str, orcid_id: str) -> str | None:
        """Attempt to fetch email from the ORCID public API.

        Returns the primary email if available, or None.
        Failures are logged and swallowed — email is optional.
        """
        # The record API lives at pub.orcid.org, not orcid.org.
        # For the sandbox, replace orcid.org → sandbox.orcid.org in the base,
        # then derive the pub API host accordingly.
        orcid_base = get_settings().auth.orcid_api_base  # e.g. https://orcid.org
        # pub.orcid.org for production, pub.sandbox.orcid.org for sandbox
        pub_api_base = orcid_base.replace("://orcid.org", "://pub.orcid.org").replace(
            "://sandbox.orcid.org", "://pub.sandbox.orcid.org"
        )
        api_url = f"{pub_api_base}/v3.0/{orcid_id}/email"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(api_url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                emails = data.get("email", [])
                if not emails:
                    return None

                # Prefer primary email
                for e in emails:
                    if e.get("primary"):
                        return str(e["email"])
                return str(emails[0]["email"])
        except Exception:
            logger.debug("Could not fetch email from ORCID API for %s", orcid_id, exc_info=True)
            return None
