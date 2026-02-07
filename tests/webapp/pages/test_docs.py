"""Integration tests for documentation page."""

from unittest.mock import patch

import pytest
from nicegui.testing import user_simulation


class TestDocsPage:
    """Tests for documentation page rendering."""

    @pytest.mark.asyncio
    async def test_docs_page_renders(self):
        """Test that docs page renders with content."""
        with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
            from shandy.webapp_components.pages.docs import docs_page

            async with user_simulation(root=docs_page) as user:
                await user.open("/")

                # Should see documentation header
                await user.should_see(content="SHANDY - Documentation")
                await user.should_see(content="Back to Jobs")

    @pytest.mark.asyncio
    async def test_docs_content_present(self):
        """Test that documentation content is rendered."""
        with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
            from shandy.webapp_components.pages.docs import docs_page

            async with user_simulation(root=docs_page) as user:
                await user.open("/")

                # Check for key documentation sections
                await user.should_see(content="# SHANDY Documentation")
                await user.should_see(content="What is SHANDY?")
                await user.should_see(content="How It Works")
                await user.should_see(content="Submit a Job")

    @pytest.mark.asyncio
    async def test_docs_examples_section(self):
        """Test that documentation includes key sections."""
        with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
            from nicegui.elements.markdown import Markdown

            from shandy.webapp_components.pages.docs import docs_page

            async with user_simulation(root=docs_page) as user:
                await user.open("/")

                # Check for Markdown element with documentation content
                await user.should_see(kind=Markdown)

    @pytest.mark.asyncio
    async def test_docs_has_back_button(self):
        """Test that docs page has navigation button."""
        with patch("shandy.webapp_components.utils.auth.DISABLE_AUTH", True):
            from shandy.webapp_components.pages.docs import docs_page

            async with user_simulation(root=docs_page) as user:
                await user.open("/")

                # Should have back button
                await user.should_see(content="Back to Jobs")
