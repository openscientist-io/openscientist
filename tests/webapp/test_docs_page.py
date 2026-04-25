"""Tests for documentation page content."""

from openscientist.webapp_components.pages.docs import DOCS_PAGE_CONTENT_MARKDOWN


def test_docs_markdown_separates_intro_text_from_timeline_list() -> None:
    """Timeline bullets should be separated from the lead-in paragraph."""
    assert "Each iteration shows:\n\n- What the agent investigated" in DOCS_PAGE_CONTENT_MARKDOWN


def test_docs_markdown_separates_report_intro_from_bullets() -> None:
    """Report bullets should render as a proper list."""
    assert "The final scientific report includes:\n\n- Summary." in DOCS_PAGE_CONTENT_MARKDOWN


def test_docs_markdown_keeps_long_numbered_item_as_same_list_entry() -> None:
    """Wrapped numbered items should stay indented as part of the list item."""
    assert (
        "2. **Clean Data**: Ensure files are properly formatted. Provide a detailed explanation of the\n"
        "   data file in your query when possible"
    ) in DOCS_PAGE_CONTENT_MARKDOWN
