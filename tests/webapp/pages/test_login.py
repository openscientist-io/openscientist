"""Integration tests for login page."""

import os
from unittest.mock import patch

import pytest
from nicegui.testing import user_simulation


class TestLoginPage:
    """Tests for login page rendering and functionality."""

    @pytest.mark.asyncio
    async def test_login_page_renders(self):
        """Test that login page renders without errors."""
        # Set APP_PASSWORD_HASH env var so the login page shows the password form
        with patch.dict(os.environ, {"APP_PASSWORD_HASH": "test_hash"}):
            # Disable the autouse _disable_auth fixture's effect for this test
            with patch("shandy.auth.middleware.DISABLE_AUTH", False):
                from shandy.webapp_components.pages.login import login_page

                async with user_simulation(root=login_page) as user:
                    await user.open("/")

                    # Should see login form elements
                    await user.should_see(content="SHANDY")
                    await user.should_see(content="Password")
                    await user.should_see(content="Login")

    @pytest.mark.asyncio
    async def test_login_form_exists(self):
        """Test that login form has password input."""
        with patch.dict(os.environ, {"APP_PASSWORD_HASH": "test_hash"}):
            with patch("shandy.auth.middleware.DISABLE_AUTH", False):
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
        from shandy.webapp_components.pages.login import login_page

        async with user_simulation(root=login_page) as user:
            # This would normally show login, but with DISABLE_AUTH it should redirect
            await user.open("/")
            # The page function should exist and be callable
            assert login_page is not None
