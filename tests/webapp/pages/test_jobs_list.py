"""Integration tests for jobs list page."""

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from nicegui.testing import user_simulation

from shandy.database.models import Job, Session
from shandy.job_manager import JobInfo, JobManager


class TestJobsListPage:
    """Tests for jobs list page."""

    session_token: str
    job_manager: JobManager

    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, webapp_session: Session, job_manager: JobManager):
        """Set up common test fixtures as class attributes."""
        self.session_token = str(webapp_session.id)
        self.job_manager = job_manager
        yield

    @pytest.mark.asyncio
    async def test_jobs_list_page_renders(self):
        """Test that jobs list page renders."""
        from shandy.webapp_components.pages.jobs_list import jobs_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=jobs_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/jobs")

                # Should see jobs list header and summary cards
                await browser.should_see("SHANDY - Jobs")
                await browser.should_see("Total Jobs")
                await browser.should_see("Running")

    @pytest.mark.asyncio
    async def test_jobs_list_with_no_jobs(self):
        """Test jobs list when no jobs exist."""
        from shandy.webapp_components.pages.jobs_list import jobs_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=jobs_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/jobs")

                # Should show summary cards even when empty
                await browser.should_see("SHANDY - Jobs")
                await browser.should_see("Total Jobs")

    @pytest.mark.asyncio
    async def test_jobs_list_with_jobs(
        self,
        webapp_job_completed: tuple[Job, JobInfo, Path],
        webapp_job_running: tuple[Job, JobInfo, Path],
    ):
        """Test jobs list when jobs exist."""
        from shandy.webapp_components.pages.jobs_list import jobs_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=jobs_page) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/jobs")

                # Should show summary with total jobs count
                await browser.should_see("Total Jobs")
