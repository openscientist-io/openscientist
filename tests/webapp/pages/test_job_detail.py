"""Integration tests for job detail page."""

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from nicegui.testing import user_simulation

from shandy.database.models import Job, Session
from shandy.job_manager import JobInfo, JobManager


class TestJobDetailPage:
    """Tests for job detail page."""

    session_token: str
    job_manager: JobManager

    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, webapp_session: Session, job_manager: JobManager):
        """Set up common test fixtures as class attributes."""
        self.session_token = str(webapp_session.id)
        self.job_manager = job_manager
        yield

    @pytest.mark.asyncio
    async def test_job_not_found(self):
        """Test handling of non-existent job."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page("nonexistent")) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open("/job/nonexistent")
                await browser.should_see("Job nonexistent not found")
                await browser.should_see("Back to Jobs")

    @pytest.mark.asyncio
    async def test_pending_job_renders(self, webapp_job_pending: tuple[Job, JobInfo, Path]):
        """Test that a pending job renders correctly."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_info, job_dir = webapp_job_pending

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page(str(job.id))) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open(f"/job/{job.id}")
                await browser.should_see(f"SHANDY - {job.id}")
                await browser.should_see("Back to Jobs")
                await browser.should_see("Research Log")
                await browser.should_see("Report")
                await browser.should_see("Status")
                await browser.should_see(job.title)

    @pytest.mark.asyncio
    async def test_running_job_shows_iterations(
        self, webapp_job_running: tuple[Job, JobInfo, Path]
    ):
        """Test that a running job displays iteration timeline."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_info, job_dir = webapp_job_running

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page(str(job.id))) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open(f"/job/{job.id}")
                await browser.should_see(job.title)
                await browser.should_see("Progress")
                await browser.should_see("Findings")
                await browser.should_see("Investigation Timeline")
                await browser.should_see("Found direct binding evidence")
                await browser.should_see("Explored functional implications")
                await browser.should_see("searches")
                await browser.should_see("findings")

    @pytest.mark.asyncio
    async def test_failed_job_shows_error(self, webapp_job_failed: tuple[Job, JobInfo, Path]):
        """Test that a failed job displays error information."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_info, job_dir = webapp_job_failed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page(str(job.id))) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open(f"/job/{job.id}")
                await browser.should_see("Error")
                await browser.should_see("API request failed")
                await browser.should_see("Rate limit")

    @pytest.mark.asyncio
    async def test_job_detail_shows_literature_count(
        self, webapp_job_running: tuple[Job, JobInfo, Path]
    ):
        """Test that literature count is displayed correctly."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_info, job_dir = webapp_job_running

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page(str(job.id))) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open(f"/job/{job.id}")
                await browser.should_see("Papers Reviewed")

    @pytest.mark.asyncio
    async def test_job_detail_handles_missing_knowledge_state(
        self, webapp_job_pending: tuple[Job, JobInfo, Path]
    ):
        """Test that page handles missing knowledge state gracefully."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_info, job_dir = webapp_job_pending

        # Delete the knowledge state file to simulate missing file
        ks_path = job_dir / "knowledge_state.json"
        if ks_path.exists():
            ks_path.unlink()

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page(str(job.id))) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open(f"/job/{job.id}")
                await browser.should_see(f"SHANDY - {job.id}")
                await browser.should_see("Knowledge graph not found")

    @pytest.mark.asyncio
    async def test_job_detail_completed_has_download_buttons(
        self, webapp_job_completed: tuple[Job, JobInfo, Path]
    ):
        """Test that completed job shows download buttons in report tab."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_info, job_dir = webapp_job_completed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page(str(job.id))) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open(f"/job/{job.id}")
                await browser.should_see("Report")

    @pytest.mark.asyncio
    async def test_job_detail_shows_status_badge_colors(
        self, webapp_job_completed: tuple[Job, JobInfo, Path]
    ):
        """Test that status badges are displayed for different job states."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_info, job_dir = webapp_job_completed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page(str(job.id))) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open(f"/job/{job.id}")
                await browser.should_see("Status")

    @pytest.mark.asyncio
    async def test_job_with_findings_displays_count(
        self, webapp_job_completed: tuple[Job, JobInfo, Path]
    ):
        """Test that findings count is displayed."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_info, job_dir = webapp_job_completed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            mock_get_jm.return_value = self.job_manager

            async with user_simulation(root=lambda: job_detail_page(str(job.id))) as browser:
                browser.http_client.cookies.set("session_token", self.session_token)
                await browser.open(f"/job/{job.id}")
                await browser.should_see("Findings")
                await browser.should_see("3")
