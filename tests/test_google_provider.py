"""Tests for the Google OAuth provider user-info extraction.

Uses recorded Google userinfo API shapes and fakes HTTP at the provider
boundary, as the ORCID provider tests do.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from openscientist.auth.providers.google import GoogleProvider


def _fake_client(userinfo_payload: dict) -> object:
    """Stand-in for the Google userinfo call of get_user_info."""
    response = AsyncMock(spec=httpx.Response)
    response.raise_for_status = lambda: None
    response.json.return_value = userinfo_payload

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=response)
    return mock_client


@pytest.mark.asyncio
async def test_verified_email_is_reported_verified():
    """A userinfo response with email_verified=true reports verified."""
    mock_client = _fake_client(
        {
            "sub": "1076597654689",
            "email": "grace@example.com",
            "email_verified": True,
            "name": "Grace Hopper",
        }
    )

    with patch("openscientist.auth.providers.google.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await GoogleProvider.get_user_info({"access_token": "tok"})

    assert result["provider_user_id"] == "1076597654689"
    assert result["email"] == "grace@example.com"
    assert result["name"] == "Grace Hopper"
    assert result["email_verified"] is True


@pytest.mark.asyncio
async def test_unverified_email_is_reported_unverified():
    """A userinfo response with email_verified=false reports unverified."""
    mock_client = _fake_client(
        {
            "sub": "1076597654689",
            "email": "grace@example.com",
            "email_verified": False,
            "name": "Grace Hopper",
        }
    )

    with patch("openscientist.auth.providers.google.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await GoogleProvider.get_user_info({"access_token": "tok"})

    assert result["email"] == "grace@example.com"
    assert result["email_verified"] is False


@pytest.mark.asyncio
async def test_missing_email_falls_back_to_unverified_invalid():
    """A userinfo response without an email falls back to an unverified
    address."""
    mock_client = _fake_client({"sub": "1076597654689", "name": "Grace Hopper"})

    with patch("openscientist.auth.providers.google.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await GoogleProvider.get_user_info({"access_token": "tok"})

    assert result["email"] == "1076597654689@google.invalid"
    assert result["email_verified"] is False


@pytest.mark.asyncio
async def test_missing_access_token_raises_value_error():
    """Missing access_token raises ValueError."""
    with pytest.raises(ValueError, match="No access_token"):
        await GoogleProvider.get_user_info({"sub": "no-token"})
