"""Integration tests for new job page."""

from unittest.mock import patch

import pytest
import pytest_asyncio
from nicegui.testing import user_simulation

from shandy.database.models import Session
from shandy.job_manager import JobManager


class TestNewJobPage:
    """Tests for new job creation page."""

    session_token: str
    job_manager: JobManager

    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, webapp_session: Session, job_manager: JobManager):
        """Set up common test fixtures as class attributes."""
        self.session_token = str(webapp_session.id)
        self.job_manager = job_manager
        yield

    @pytest.mark.asyncio
    async def test_new_job_page_renders(self):
        """Test that new job page renders with form."""
        from shandy.webapp_components.pages.new_job import new_job_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=new_job_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/new")

                # Should see new job form elements
                await browser.should_see("Submit Discovery Job")
                await browser.should_see("Research Question")
                await browser.should_see("Start Discovery")

    @pytest.mark.asyncio
    async def test_new_job_form_fields(self):
        """Test that form has all required fields."""
        from shandy.webapp_components.pages.new_job import new_job_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=new_job_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/new")

                # Check for form fields
                await browser.should_see("Research Question")
                await browser.should_see("Max Iterations")
                await browser.should_see("Upload Data Files")

    @pytest.mark.asyncio
    async def test_new_job_navigation_buttons(self):
        """Test that page has navigation buttons."""
        from shandy.webapp_components.pages.new_job import new_job_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=new_job_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/new")

                # Should have navigation buttons
                await browser.should_see("View Jobs")
                await browser.should_see("Documentation")

    @pytest.mark.asyncio
    async def test_new_job_advanced_options(self):
        """Test that advanced options section exists."""
        from shandy.webapp_components.pages.new_job import new_job_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=new_job_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/new")

                # Should show advanced options
                await browser.should_see("Advanced Options (Experimental)")
                await browser.should_see("Coinvestigate Mode")
