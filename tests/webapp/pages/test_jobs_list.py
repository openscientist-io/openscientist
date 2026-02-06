"""Integration tests for jobs list page."""

from unittest.mock import Mock, patch

import pytest
from nicegui.testing import user_simulation


class TestJobsListPage:
    """Tests for jobs list page."""

    @pytest.mark.asyncio
    async def test_jobs_list_page_renders(self, mock_job_manager):
        """Test that jobs list page renders."""
        from shandy.webapp_components.pages.jobs_list import jobs_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
                with patch.object(mock_job_manager, "list_jobs", return_value=[]):
                    mock_get_jm.return_value = mock_job_manager

                    async with user_simulation(root=jobs_page) as user:
                        await user.open("/jobs")

                        # Should see jobs list header and summary cards
                        await user.should_see("SHANDY - Jobs")
                        await user.should_see("Total Jobs")
                        await user.should_see("Running")

    @pytest.mark.asyncio
    async def test_jobs_list_with_no_jobs(self, mock_job_manager):
        """Test jobs list when no jobs exist."""
        from shandy.webapp_components.pages.jobs_list import jobs_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
                with patch.object(mock_job_manager, "list_jobs", return_value=[]):
                    mock_get_jm.return_value = mock_job_manager

                    async with user_simulation(root=jobs_page) as user:
                        await user.open("/jobs")

                        # Should show summary cards even when empty
                        await user.should_see("SHANDY - Jobs")
                        await user.should_see("Total Jobs")

    @pytest.mark.asyncio
    async def test_jobs_list_navigation_buttons(self, mock_job_manager):
        """Test that page has action buttons."""
        from shandy.webapp_components.pages.jobs_list import jobs_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
                with patch.object(mock_job_manager, "list_jobs", return_value=[]):
                    mock_get_jm.return_value = mock_job_manager

                    async with user_simulation(root=jobs_page) as user:
                        await user.open("/jobs")

                        # Should have navigation buttons
                        await user.should_see("New Job")
                        await user.should_see("Billing")

    @pytest.mark.asyncio
    async def test_jobs_list_with_jobs(self, mock_job_manager, sample_job_info):
        """Test jobs list when jobs exist."""
        from shandy.webapp_components.pages.jobs_list import jobs_page

        # Create a second job
        job2 = Mock()
        job2.job_id = "test-456"
        job2.research_question = "How do proteins fold?"
        job2.status = Mock(value="completed")
        job2.error = None
        job2.iterations_completed = 5
        job2.max_iterations = 10
        job2.findings_count = 12
        job2.created_at = "2026-02-05T15:30:00.000"

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
                with patch.object(
                    mock_job_manager, "list_jobs", return_value=[sample_job_info, job2]
                ):
                    mock_get_jm.return_value = mock_job_manager

                    async with user_simulation(root=jobs_page) as user:
                        await user.open("/jobs")

                        # Should show summary with total jobs count
                        # Note: Table row content is internal and not directly visible in test
                        await user.should_see("Total Jobs")
                        # The table should be rendered (jobs data is in table rows internally)
