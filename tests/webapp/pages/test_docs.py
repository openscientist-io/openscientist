"""Integration tests for documentation page."""

import pytest
import pytest_asyncio
from nicegui.testing import user_simulation

from shandy.database.models import Session


class TestDocsPage:
    """Tests for documentation page rendering."""

    session_token: str

    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, webapp_session: Session):
        """Set up common test fixtures as class attributes."""
        self.session_token = str(webapp_session.id)
        yield

    @pytest.mark.asyncio
    async def test_docs_page_renders(self):
        """Test that docs page renders with content."""
        from shandy.webapp_components.pages.docs import docs_page

        async with user_simulation(root=docs_page) as browser:
            browser.http_client.cookies.set("session_token", self.session_token)
            await browser.open("/")

            # Should see documentation header
            await browser.should_see(content="SHANDY - Documentation")
            await browser.should_see(content="Back to Jobs")

    @pytest.mark.asyncio
    async def test_docs_content_present(self):
        """Test that documentation content is rendered."""
        from shandy.webapp_components.pages.docs import docs_page

        async with user_simulation(root=docs_page) as browser:
            browser.http_client.cookies.set("session_token", self.session_token)
            await browser.open("/")

            # Check for key documentation sections
            await browser.should_see(content="# SHANDY Documentation")
            await browser.should_see(content="What is SHANDY?")
            await browser.should_see(content="How It Works")
            await browser.should_see(content="Submit a Job")
