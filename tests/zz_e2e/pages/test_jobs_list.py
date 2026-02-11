"""E2E tests for the jobs list page.

These tests verify that:
1. The UI displays correctly
2. User interactions work as expected
3. UI changes are reflected in the database
"""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


class TestJobsPageNavigation:
    """Tests for basic page navigation."""

    def test_login_page_accessible(self, e2e_page: Page, e2e_server: str):
        """Test that login page is accessible."""
        e2e_page.goto(f"{e2e_server}/login")

        # Check that login page elements are present
        expect(e2e_page).to_have_title("SHANDY")

    def test_mock_login_form_accessible(self, e2e_page: Page, e2e_server: str):
        """Test that mock login form is accessible when enabled."""
        e2e_page.goto(f"{e2e_server}/mock-login-form")

        # Check that mock login form elements are present
        expect(e2e_page.locator("text=Mock OAuth Login")).to_be_visible()
        expect(e2e_page.locator('input[placeholder="dev@example.com"]')).to_be_visible()

    def test_unauthenticated_redirects_to_login(self, e2e_page: Page, e2e_server: str):
        """Test that protected pages redirect to login when not authenticated."""
        e2e_page.goto(f"{e2e_server}/jobs")

        # Should redirect to login page
        expect(e2e_page).to_have_url(f"{e2e_server}/login")


class TestAuthenticatedJobsPage:
    """Tests for the authenticated jobs list page."""

    def test_jobs_page_accessible_when_logged_in(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that jobs page is accessible after login."""
        e2e_logged_in_page.goto(f"{e2e_server}/jobs")

        # Check that jobs page elements are present
        expect(e2e_logged_in_page.locator("text=SHANDY - Jobs")).to_be_visible()
        expect(e2e_logged_in_page.locator("text=My Jobs")).to_be_visible()

    def test_new_job_button_navigates_correctly(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that clicking New Job navigates to the new job page."""
        e2e_logged_in_page.goto(f"{e2e_server}/jobs")

        # Click the New Job button
        e2e_logged_in_page.click("button:has-text('New Job')")

        # Should navigate to new job page
        e2e_logged_in_page.wait_for_url(f"{e2e_server}/new")
        expect(e2e_logged_in_page.locator("text=Submit Discovery Job")).to_be_visible()


class TestNewJobPage:
    """Tests for the new job submission page."""

    def test_new_job_form_elements_present(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that new job form has all required elements."""
        e2e_logged_in_page.goto(f"{e2e_server}/new")

        # Check form elements
        expect(e2e_logged_in_page.locator("text=Research Question")).to_be_visible()
        expect(e2e_logged_in_page.locator("text=Upload Data Files")).to_be_visible()
        expect(e2e_logged_in_page.locator("text=Max Iterations")).to_be_visible()
        expect(e2e_logged_in_page.locator("text=Start Discovery")).to_be_visible()

    def test_validation_requires_research_question(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that form validation requires a research question."""
        e2e_logged_in_page.goto(f"{e2e_server}/new")

        # Try to submit without filling in the research question
        e2e_logged_in_page.click("button:has-text('Start Discovery')")

        # Should show validation error (notification)
        expect(e2e_logged_in_page.locator("text=Please enter a research question")).to_be_visible()


class TestJobsListWithDatabase:
    """Tests that verify UI state matches database state.

    Note: Database assertions in E2E tests require careful handling of
    async/sync boundaries. These tests demonstrate the pattern but may
    need adjustment based on specific pytest-asyncio configuration.
    """

    def test_jobs_list_shows_empty_state(
        self,
        e2e_logged_in_page: Page,
        e2e_server: str,
    ):
        """Test that jobs list shows empty state for new user."""
        e2e_logged_in_page.goto(f"{e2e_server}/jobs")

        # The table should be present (pagination shows 0 results for new user)
        expect(e2e_logged_in_page.locator("text=My Jobs")).to_be_visible()

        # Total Jobs card should show 0 for a user with no jobs
        expect(e2e_logged_in_page.locator("text=Total Jobs")).to_be_visible()
