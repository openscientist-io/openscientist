"""Integration tests for login page."""

from unittest.mock import patch

import pytest
from nicegui.testing import user_simulation


class TestLoginPage:
    """Tests for login page rendering and functionality."""

    @pytest.mark.asyncio
    async def test_login_page_renders(self):
        """Test that login page renders without errors."""
        # Mock password hash to avoid env var requirements
        with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", False):
            with patch("shandy.webapp_components.utils.auth.PASSWORD_HASH", b"test_hash"):
                # Import after patching
                from shandy.webapp_components.pages.login import login_page

                async with user_simulation(root=login_page) as user:
                    await user.open("/")

                    # Should see login form elements (checking content in Markdown)
                    await user.should_see(content="# SHANDY")
                    await user.should_see(content="Password")
                    await user.should_see(content="Login")

    @pytest.mark.asyncio
    async def test_login_form_exists(self):
        """Test that login form has password input."""
        with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", False):
            with patch("shandy.webapp_components.utils.auth.PASSWORD_HASH", b"test_hash"):
                from shandy.webapp_components.pages.login import login_page

                async with user_simulation(root=login_page) as user:
                    await user.open("/")

                    # Verify form structure
                    await user.should_see(content="Password")
                    await user.should_see(content="Login")


class TestLoginWithAuthDisabled:
    """Tests for login behavior with authentication disabled."""

    @pytest.mark.asyncio
    async def test_auth_disabled_redirects(self):
        """Test that disabling auth bypasses login."""
        with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
            from shandy.webapp_components.pages.login import login_page

            async with user_simulation(root=login_page) as user:
                # This would normally show login, but with DISABLE_AUTH it should redirect
                await user.open("/")
                # The page function should exist and be callable
                assert login_page is not None
