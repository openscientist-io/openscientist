"""
Tests for ORCID OAuth provider implementation.

Tests user info extraction from ORCID token responses, including
email resolution fallback chain and name handling.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from openscientist.auth.providers.orcid import OrcidProvider


@pytest.mark.asyncio
async def test_full_token_with_oidc_email():
    """Token with ORCID iD, name, and OIDC email claim."""
    token = {
        "access_token": "test-token",
        "orcid": "0000-0002-1234-5678",
        "name": "Jane Researcher",
        "userinfo": {"email": "jane@university.edu"},
    }

    result = await OrcidProvider.get_user_info(token)

    assert result["provider_user_id"] == "0000-0002-1234-5678"
    assert result["email"] == "jane@university.edu"
    assert result["name"] == "Jane Researcher"
    assert result["email_verified"] is True


@pytest.mark.asyncio
async def test_no_oidc_email_falls_back_to_api():
    """No OIDC email — API returns verified primary email."""
    token = {
        "access_token": "test-token",
        "orcid": "0000-0002-1234-5678",
        "name": "Jane Researcher",
    }

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.raise_for_status = lambda: None
    mock_response.json.return_value = {
        "email": [
            {"email": "jane@university.edu", "verified": True, "primary": True},
        ]
    }

    with patch("openscientist.auth.providers.orcid.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await OrcidProvider.get_user_info(token)

    assert result["email"] == "jane@university.edu"
    assert result["email_verified"] is True
    assert result["name"] == "Jane Researcher"


@pytest.mark.asyncio
async def test_no_email_anywhere_falls_back_to_invalid():
    """No email from OIDC or API — falls back to @orcid.invalid."""
    token = {
        "access_token": "test-token",
        "orcid": "0000-0002-1234-5678",
        "name": "Private User",
    }

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.raise_for_status = lambda: None
    mock_response.json.return_value = {"email": []}

    with patch("openscientist.auth.providers.orcid.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await OrcidProvider.get_user_info(token)

    assert result["email"] == "0000-0002-1234-5678@orcid.invalid"
    assert result["email_verified"] is False


@pytest.mark.asyncio
async def test_missing_access_token_raises_value_error():
    """Missing access_token raises ValueError."""
    token = {
        "orcid": "0000-0002-1234-5678",
        "name": "No Token User",
    }

    with pytest.raises(ValueError, match="No access_token"):
        await OrcidProvider.get_user_info(token)


@pytest.mark.asyncio
async def test_email_api_failure_falls_back_gracefully():
    """Email API failure — logs warning, falls back to @orcid.invalid."""
    token = {
        "access_token": "test-token",
        "orcid": "0000-0002-1234-5678",
        "name": "API Failure User",
    }

    with patch("openscientist.auth.providers.orcid.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Server Error",
                request=httpx.Request("GET", "https://pub.orcid.org/v3.0/test/email"),
                response=httpx.Response(500),
            )
        )
        mock_client_cls.return_value = mock_client

        result = await OrcidProvider.get_user_info(token)

    assert result["email"] == "0000-0002-1234-5678@orcid.invalid"
    assert result["email_verified"] is False
    assert result["name"] == "API Failure User"


@pytest.mark.asyncio
async def test_missing_name_falls_back_to_orcid_id():
    """Missing name — falls back to ORCID iD string."""
    token = {
        "access_token": "test-token",
        "orcid": "0000-0002-1234-5678",
        "name": "",
        "userinfo": {"email": "private@example.com"},
    }

    result = await OrcidProvider.get_user_info(token)

    assert result["name"] == "0000-0002-1234-5678"
    assert result["email"] == "private@example.com"


@pytest.mark.asyncio
async def test_name_from_userinfo_given_family():
    """Name assembled from userinfo given_name + family_name when token name is empty."""
    token = {
        "access_token": "test-token",
        "orcid": "0000-0002-1234-5678",
        "userinfo": {
            "given_name": "Marie",
            "family_name": "Curie",
            "email": "marie@example.com",
        },
    }

    result = await OrcidProvider.get_user_info(token)

    assert result["name"] == "Marie Curie"


@pytest.mark.asyncio
async def test_unverified_email_skipped_in_api():
    """API email records without verified=True are skipped."""
    token = {
        "access_token": "test-token",
        "orcid": "0000-0002-1234-5678",
        "name": "Unverified User",
    }

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.raise_for_status = lambda: None
    mock_response.json.return_value = {
        "email": [
            {"email": "unverified@example.com", "verified": False, "primary": True},
        ]
    }

    with patch("openscientist.auth.providers.orcid.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await OrcidProvider.get_user_info(token)

    assert result["email"] == "0000-0002-1234-5678@orcid.invalid"
    assert result["email_verified"] is False
