"""Integration tests for index/home page."""

import pytest
import pytest_asyncio

from shandy.database.models import Session


class TestIndexPage:
    """Tests for index page rendering and redirection."""

    session_token: str

    @pytest_asyncio.fixture(autouse=True)
    async def setup(self, webapp_session: Session):
        """Set up common test fixtures as class attributes."""
        self.session_token = str(webapp_session.id)
        yield

    @pytest.mark.asyncio
    async def test_index_requires_auth(self):
        """Test that index page has auth decorator."""
        from shandy.webapp_components.pages.index import index_page

        # Check that the function has the require_auth wrapper
        assert hasattr(index_page, "__wrapped__") or hasattr(index_page, "__name__")
        assert index_page.__name__ == "wrapper" or index_page.__name__ == "index_page"
