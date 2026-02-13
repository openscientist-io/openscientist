"""Integration tests for billing page."""

from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from nicegui.testing import user_simulation

from shandy.database.models import Session


class TestBillingPage:
    """Tests for billing page."""

    session_token: str
    mock_provider: Any

    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, webapp_session: Session, mock_provider: Any):
        """Set up common test fixtures as class attributes."""
        self.session_token = str(webapp_session.id)
        self.mock_provider = mock_provider
        yield

    @pytest.mark.asyncio
    async def test_billing_page_renders(self):
        """Test that billing page renders."""
        from shandy.webapp_components.pages.billing import billing_page

        with patch("shandy.providers.get_provider") as mock_get_provider:
            mock_get_provider.return_value = self.mock_provider

            async with user_simulation(root=billing_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/billing")

                # Should see billing page header
                await browser.should_see("SHANDY - Billing")
                await browser.should_see("Project Costs")

    @pytest.mark.asyncio
    async def test_billing_navigation(self):
        """Test navigation buttons on billing page."""
        from shandy.webapp_components.pages.billing import billing_page

        with patch("shandy.providers.get_provider") as mock_get_provider:
            mock_get_provider.return_value = self.mock_provider

            async with user_simulation(root=billing_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/billing")

                # Should have back button
                await browser.should_see("Back to Jobs")
