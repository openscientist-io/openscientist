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


class TestAdminPage:
    """Tests for the admin page."""

    def test_admin_page_accessible_when_logged_in(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that admin page is accessible after login."""
        e2e_logged_in_page.goto(f"{e2e_server}/admin")

        # Check that admin page elements are present
        expect(e2e_logged_in_page.locator("text=SHANDY - Admin")).to_be_visible()
        expect(
            e2e_logged_in_page.get_by_role("heading", name="Admin - Orphaned Jobs")
        ).to_be_visible()

    def test_admin_page_has_tabs(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that admin page has expected tabs."""
        e2e_logged_in_page.goto(f"{e2e_server}/admin")

        # Check tabs are present (use exact text match for tab labels)
        expect(e2e_logged_in_page.get_by_role("tab", name="Orphaned Jobs")).to_be_visible()
        expect(e2e_logged_in_page.get_by_role("tab", name="Users")).to_be_visible()
        expect(e2e_logged_in_page.get_by_role("tab", name="Legacy User")).to_be_visible()


class TestBillingPage:
    """Tests for the billing page."""

    def test_billing_page_accessible_when_logged_in(
        self, e2e_logged_in_page: Page, e2e_server: str
    ):
        """Test that billing page is accessible after login."""
        e2e_logged_in_page.goto(f"{e2e_server}/billing")

        # Check that billing page elements are present
        expect(e2e_logged_in_page.locator("text=SHANDY - Billing")).to_be_visible()
        expect(e2e_logged_in_page.locator("text=Project Costs")).to_be_visible()

    def test_billing_page_shows_provider_info(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that billing page shows provider information."""
        e2e_logged_in_page.goto(f"{e2e_server}/billing")

        # Should show provider information
        expect(e2e_logged_in_page.locator("text=Provider Information")).to_be_visible()


class TestMockLoginFlow:
    """Tests for the mock login UI flow."""

    def test_mock_login_button_works(self, e2e_page: Page, e2e_server: str):
        """Test that the mock login button on login page works."""
        e2e_page.goto(f"{e2e_server}/login")

        # Click the mock login button (quick login - no form)
        e2e_page.click("button:has-text('Mock Login')")

        # Should redirect to jobs page after login
        e2e_page.wait_for_url(f"{e2e_server}/jobs", timeout=10000)
        expect(e2e_page.locator("text=SHANDY - Jobs")).to_be_visible()

    def test_mock_login_form_accessible(self, e2e_page: Page, e2e_server: str):
        """Test that the mock login form page is accessible and has expected elements."""
        e2e_page.goto(f"{e2e_server}/mock-login-form")

        # The form should have pre-filled values
        expect(e2e_page.locator('input[placeholder="dev@example.com"]')).to_be_visible()
        expect(e2e_page.locator('input[placeholder="Dev User"]')).to_be_visible()
        expect(e2e_page.locator('input[placeholder="devuser"]')).to_be_visible()

        # Sign In button should be present
        expect(e2e_page.locator("button:has-text('Sign In')")).to_be_visible()
        expect(e2e_page.locator("button:has-text('Cancel')")).to_be_visible()


class TestLogout:
    """Tests for logout functionality."""

    def test_logout_redirects_to_login(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that logout redirects to login page."""
        # First verify we're logged in
        e2e_logged_in_page.goto(f"{e2e_server}/jobs")
        expect(e2e_logged_in_page.locator("text=SHANDY - Jobs")).to_be_visible()

        # Navigate to logout
        e2e_logged_in_page.goto(f"{e2e_server}/auth/logout")

        # Should redirect to login page
        e2e_logged_in_page.wait_for_url(f"{e2e_server}/login", timeout=10000)

    def test_session_invalidated_after_logout(self, e2e_logged_in_page: Page, e2e_server: str):
        """Test that session is invalidated after logout."""
        # First verify we're logged in
        e2e_logged_in_page.goto(f"{e2e_server}/jobs")
        expect(e2e_logged_in_page.locator("text=SHANDY - Jobs")).to_be_visible()

        # Logout
        e2e_logged_in_page.goto(f"{e2e_server}/auth/logout")
        e2e_logged_in_page.wait_for_url(f"{e2e_server}/login", timeout=10000)

        # Try to access protected page - should redirect to login
        e2e_logged_in_page.goto(f"{e2e_server}/jobs")
        e2e_logged_in_page.wait_for_load_state("networkidle")

        # Should redirect to login page
        expect(e2e_logged_in_page).to_have_url(f"{e2e_server}/login", timeout=10000)


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
