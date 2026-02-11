"""Integration tests for billing page."""

from unittest.mock import patch

import pytest
from nicegui.testing import user_simulation


class TestBillingPage:
    """Tests for billing page."""

    @pytest.mark.asyncio
    async def test_billing_page_renders(self, mock_provider):
        """Test that billing page renders."""
        from shandy.webapp_components.pages.billing import billing_page

        with patch("shandy.providers.get_provider") as mock_get_provider:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_get_provider.return_value = mock_provider

                async with user_simulation(root=billing_page) as user:
                    await user.open("/billing")

                    # Should see billing page header
                    await user.should_see("SHANDY - Billing")
                    await user.should_see("Project Costs")

    @pytest.mark.asyncio
    async def test_billing_navigation(self, mock_provider):
        """Test navigation buttons on billing page."""
        from shandy.webapp_components.pages.billing import billing_page

        with patch("shandy.providers.get_provider") as mock_get_provider:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_get_provider.return_value = mock_provider

                async with user_simulation(root=billing_page) as user:
                    await user.open("/billing")

                    # Should have back button
                    await user.should_see("Back to Jobs")
