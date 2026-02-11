"""Integration tests for new job page."""

from unittest.mock import patch

import pytest
from nicegui.testing import user_simulation


class TestNewJobPage:
    """Tests for new job creation page."""

    @pytest.mark.asyncio
    async def test_new_job_page_renders(self, mock_job_manager):
        """Test that new job page renders with form."""
        from shandy.webapp_components.pages.new_job import new_job_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=new_job_page) as user:
                    await user.open("/new")

                    # Should see new job form elements
                    await user.should_see("Submit Discovery Job")
                    await user.should_see("Research Question")
                    await user.should_see("Start Discovery")

    @pytest.mark.asyncio
    async def test_new_job_form_fields(self, mock_job_manager):
        """Test that form has all required fields."""
        from shandy.webapp_components.pages.new_job import new_job_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=new_job_page) as user:
                    await user.open("/new")

                    # Check for form fields
                    await user.should_see("Research Question")
                    await user.should_see("Max Iterations")
                    await user.should_see("Upload Data Files")

    @pytest.mark.asyncio
    async def test_new_job_navigation_buttons(self, mock_job_manager):
        """Test that page has navigation buttons."""
        from shandy.webapp_components.pages.new_job import new_job_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=new_job_page) as user:
                    await user.open("/new")

                    # Should have navigation buttons
                    await user.should_see("View Jobs")
                    await user.should_see("Documentation")

    @pytest.mark.asyncio
    async def test_new_job_advanced_options(self, mock_job_manager):
        """Test that advanced options section exists."""
        from shandy.webapp_components.pages.new_job import new_job_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=new_job_page) as user:
                    await user.open("/new")

                    # Should show advanced options
                    await user.should_see("Advanced Options (Experimental)")
                    await user.should_see("Coinvestigate Mode")
