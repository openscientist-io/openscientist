"""Tests for the mock OAuth provider used in development mode."""

import pytest

from openscientist.auth.providers.mock import MockProvider


@pytest.mark.asyncio
async def test_mock_user_info_reports_the_email_as_verified():
    """The mock identity is asserted by the operator, so it reports its
    email as verified."""
    user_info = await MockProvider.get_user_info(
        {"email": "dev@example.com", "name": "Dev User", "username": "devuser"}
    )

    assert user_info["email"] == "dev@example.com"
    assert user_info["email_verified"] is True
