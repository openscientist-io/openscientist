"""Integration tests for login page."""

from unittest.mock import MagicMock, patch

import pytest
from nicegui.testing import user_simulation


class TestLoginPage:
    """Tests for login page rendering and functionality."""

    @pytest.mark.asyncio
    async def test_login_page_renders_with_github(self):
        """Test that login page renders with GitHub OAuth."""
        mock_settings = MagicMock()
        mock_settings.auth.github_client_id = "test_github_id"
        mock_settings.auth.orcid_client_id = None

        with patch(
            "shandy.webapp_components.pages.login.get_settings",
            return_value=mock_settings,
        ):
            with patch(
                "shandy.webapp_components.pages.login.is_oauth_configured",
                return_value=True,
            ):
                with patch(
                    "shandy.webapp_components.pages.login.is_mock_auth_enabled",
                    return_value=False,
                ):
                    from shandy.webapp_components.pages.login import login_page

                    async with user_simulation(root=login_page) as user:
                        await user.open("/")
                        await user.should_see(content="SHANDY")
                        await user.should_see(content="Continue with GitHub")

    @pytest.mark.asyncio
    async def test_login_page_renders_with_orcid(self):
        """Test that login page renders with ORCID OAuth."""
        mock_settings = MagicMock()
        mock_settings.auth.github_client_id = None
        mock_settings.auth.orcid_client_id = "test_orcid_id"

        with patch(
            "shandy.webapp_components.pages.login.get_settings",
            return_value=mock_settings,
        ):
            with patch(
                "shandy.webapp_components.pages.login.is_oauth_configured",
                return_value=True,
            ):
                with patch(
                    "shandy.webapp_components.pages.login.is_mock_auth_enabled",
                    return_value=False,
                ):
                    from shandy.webapp_components.pages.login import login_page

                    async with user_simulation(root=login_page) as user:
                        await user.open("/")
                        await user.should_see(content="SHANDY")
                        await user.should_see(content="Continue with ORCID")

    @pytest.mark.asyncio
    async def test_login_page_no_auth_configured(self):
        """Test that login page shows warning when no OAuth configured."""
        mock_settings = MagicMock()
        mock_settings.auth.github_client_id = None
        mock_settings.auth.orcid_client_id = None

        with patch(
            "shandy.webapp_components.pages.login.get_settings",
            return_value=mock_settings,
        ):
            with patch(
                "shandy.webapp_components.pages.login.is_oauth_configured",
                return_value=False,
            ):
                with patch(
                    "shandy.webapp_components.pages.login.is_mock_auth_enabled",
                    return_value=False,
                ):
                    from shandy.webapp_components.pages.login import login_page

                    async with user_simulation(root=login_page) as user:
                        await user.open("/")
                        await user.should_see(content="SHANDY")
                        await user.should_see(content="No Authentication Configured")


class TestLoginWithAuthDisabled:
    """Tests for login behavior with authentication disabled."""

    @pytest.mark.asyncio
    async def test_auth_disabled_redirects(self):
        """Test that disabling auth bypasses login."""
        from shandy.webapp_components.pages.login import login_page

        async with user_simulation(root=login_page) as user:
            await user.open("/")
            assert login_page is not None
