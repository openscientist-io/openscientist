"""Tests for the GitHub OAuth provider email resolution.

Uses recorded GitHub API shapes and fakes HTTP at the provider boundary,
as the ORCID provider tests do.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from openscientist.auth.providers.github import GitHubProvider


def _fake_client(user_payload: dict, emails_payload: list[dict]) -> object:
    """Stand-ins for the two GitHub API calls of get_user_info."""
    user_response = AsyncMock(spec=httpx.Response)
    user_response.raise_for_status = lambda: None
    user_response.json.return_value = user_payload

    emails_response = AsyncMock(spec=httpx.Response)
    emails_response.raise_for_status = lambda: None
    emails_response.json.return_value = emails_payload

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=[user_response, emails_response])
    return mock_client


@pytest.mark.asyncio
async def test_verified_primary_email_is_reported_verified():
    """A verified primary record from /user/emails wins and reports verified."""
    mock_client = _fake_client(
        {"id": 7, "login": "alice", "email": None, "name": None},
        [
            {"email": "alice+work@example.com", "primary": True, "verified": True},
            {"email": "alice@example.com", "primary": False, "verified": False},
        ],
    )

    with patch("openscientist.auth.providers.github.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await GitHubProvider.get_user_info({"access_token": "tok"})

    assert result["provider_user_id"] == "7"
    assert result["email"] == "alice+work@example.com"
    assert result["email_verified"] is True


@pytest.mark.asyncio
async def test_any_verified_email_is_reported_verified():
    """A verified non-primary record is used when no verified primary exists."""
    mock_client = _fake_client(
        {"id": 8, "login": "bob", "email": None, "name": None},
        [
            {"email": "bob-private@example.com", "primary": True, "verified": False},
            {"email": "bob-work@example.com", "primary": False, "verified": True},
        ],
    )

    with patch("openscientist.auth.providers.github.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await GitHubProvider.get_user_info({"access_token": "tok"})

    assert result["email"] == "bob-work@example.com"
    assert result["email_verified"] is True


@pytest.mark.asyncio
async def test_unverified_profile_email_is_reported_unverified():
    """A public profile email only found unverified in /user/emails reports
    unverified."""
    mock_client = _fake_client(
        {"id": 9, "login": "carol", "email": "carol@example.com", "name": None},
        [
            {
                "email": "carol@example.com",
                "primary": True,
                "verified": False,
            }
        ],
    )

    with patch("openscientist.auth.providers.github.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await GitHubProvider.get_user_info({"access_token": "tok"})

    assert result["email"] == "carol@example.com"
    assert result["email_verified"] is False


@pytest.mark.asyncio
async def test_fallback_email_keeps_its_verification_flag():
    """A fallback to the primary or first /user/emails record keeps the
    record's own verified flag."""
    mock_client = _fake_client(
        {"id": 10, "login": "dave", "email": None, "name": None},
        [
            {"email": "dave-fallback@example.com", "primary": True, "verified": False},
        ],
    )

    with patch("openscientist.auth.providers.github.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await GitHubProvider.get_user_info({"access_token": "tok"})

    assert result["email"] == "dave-fallback@example.com"
    assert result["email_verified"] is False


@pytest.mark.asyncio
async def test_noreply_fallback_is_reported_unverified():
    """With no email anywhere, the noreply address is unverified."""
    mock_client = _fake_client(
        {"id": 11, "login": "erin", "email": None, "name": None},
        [],
    )

    with patch("openscientist.auth.providers.github.httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value = mock_client
        result = await GitHubProvider.get_user_info({"access_token": "tok"})

    assert result["email"] == "erin@users.noreply.github.com"
    assert result["email_verified"] is False


@pytest.mark.asyncio
async def test_missing_access_token_raises_value_error():
    """Missing access_token raises ValueError."""
    with pytest.raises(ValueError, match="No access_token"):
        await GitHubProvider.get_user_info({"login": "no-token"})
