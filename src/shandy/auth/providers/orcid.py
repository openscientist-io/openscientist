"""
ORCID OAuth provider implementation.

Handles ORCID OAuth flow and user profile extraction.
"""

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ORCIDProvider:
    """ORCID OAuth provider implementation."""

    @staticmethod
    async def get_user_info(token: dict[str, Any]) -> dict[str, Any]:
        """
        Fetch user profile from ORCID API.

        Args:
            token: OAuth token response containing access_token and orcid

        Returns:
            Dictionary with user profile:
            - provider_user_id: ORCID iD
            - email: Email address (if available)
            - name: Display name

        Raises:
            httpx.HTTPError: If API request fails
        """
        access_token = token.get("access_token")
        orcid_id = token.get("orcid")

        if not access_token or not orcid_id:
            raise ValueError("Missing access_token or orcid in token response")

        # Use production ORCID by default, sandbox for testing
        orcid_base = os.getenv("ORCID_API_BASE", "https://orcid.org")
        api_url = f"{orcid_base}/v3.0/{orcid_id}/record"

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(api_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

            # Extract name from person data
            person = data.get("person", {})
            name_data = person.get("name", {})
            given_names = name_data.get("given-names", {}).get("value", "")
            family_name = name_data.get("family-name", {}).get("value", "")
            name = f"{given_names} {family_name}".strip() or orcid_id

            # Extract email (may not be public)
            emails = person.get("emails", {}).get("email", [])
            email = None
            if emails:
                # Prefer primary email
                for e in emails:
                    if e.get("primary"):
                        email = e.get("email")
                        break
                if not email:
                    email = emails[0].get("email")

            if not email:
                logger.warning("No email found for ORCID user %s", orcid_id)
                # Use ORCID iD as fallback (not a real email)
                email = f"{orcid_id}@orcid.org"

            return {
                "provider_user_id": orcid_id,
                "email": email,
                "name": name,
                "orcid": orcid_id,
            }
