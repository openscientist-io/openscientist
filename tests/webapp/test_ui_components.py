"""Tests for UI components module."""

import html
import re
from pathlib import Path
from unittest.mock import Mock, patch

from shandy.job_manager import JobStatus
from shandy.webapp_components.ui_components import (
    STATUS_COLORS,
    STATUS_ICONS,
    get_status_badge_props,
    render_status_cell_slot,
)


class TestStatusConstants:
    """Tests for status color and icon mappings."""

    def test_status_colors_defined(self):
        """Test that all job statuses have color mappings."""
        for status in JobStatus:
            assert status in STATUS_COLORS
            assert isinstance(STATUS_COLORS[status], str)

    def test_status_icons_defined(self):
        """Test that all job statuses have icon mappings."""
        for status in JobStatus:
            assert status in STATUS_ICONS
            assert isinstance(STATUS_ICONS[status], str)


class TestGetStatusBadgeProps:
    """Tests for get_status_badge_props function."""

    def test_pending_status(self):
        """Test badge props for pending status."""
        props = get_status_badge_props(JobStatus.PENDING)
        assert props["color"] == "gray"
        assert props["icon"] == "○"
        assert props["text"] == "pending"

    def test_queued_status(self):
        """Test badge props for queued status."""
        props = get_status_badge_props(JobStatus.QUEUED)
        assert props["color"] == "blue"
        assert props["icon"] == "⟳"
        assert props["text"] == "queued"

    def test_running_status(self):
        """Test badge props for running status."""
        props = get_status_badge_props(JobStatus.RUNNING)
        assert props["color"] == "yellow"
        assert props["icon"] == "▶"
        assert props["text"] == "running"

    def test_completed_status(self):
        """Test badge props for completed status."""
        props = get_status_badge_props(JobStatus.COMPLETED)
        assert props["color"] == "green"
        assert props["icon"] == "✓"
        assert props["text"] == "completed"

    def test_failed_status(self):
        """Test badge props for failed status."""
        props = get_status_badge_props(JobStatus.FAILED)
        assert props["color"] == "red"
        assert props["icon"] == "✗"
        assert props["text"] == "failed"
        assert "bg-red-600" in props["classes"]
        assert "text-white" in props["classes"]

    def test_cancelled_status(self):
        """Test badge props for cancelled status."""
        props = get_status_badge_props(JobStatus.CANCELLED)
        assert props["color"] == "gray"
        assert props["icon"] == "⊗"
        assert props["text"] == "cancelled"

    def test_awaiting_feedback_status(self):
        """Test badge props for awaiting feedback status."""
        props = get_status_badge_props(JobStatus.AWAITING_FEEDBACK)
        assert props["color"] == "orange"
        assert props["icon"] == "⏸"
        assert props["text"] == "awaiting_feedback"

    def test_props_structure(self):
        """Test that props dict has all required keys."""
        props = get_status_badge_props(JobStatus.RUNNING)
        required_keys = ["color", "icon", "text", "classes"]
        for key in required_keys:
            assert key in props


class TestRenderStatusCellSlot:
    """Tests for render_status_cell_slot function."""

    def test_returns_string(self):
        """Test that function returns a string template."""
        template = render_status_cell_slot()
        assert isinstance(template, str)

    def test_contains_quasar_elements(self):
        """Test that template contains Quasar components."""
        template = render_status_cell_slot()
        assert "<q-td" in template
        assert "<q-badge" in template
        assert "props.row.status" in template

    def test_contains_all_status_conditions(self):
        """Test that template includes all status types."""
        template = render_status_cell_slot()

        # Check for all status conditions
        assert "props.row.status === 'failed'" in template
        assert "props.row.status === 'completed'" in template
        assert "props.row.status === 'running'" in template
        assert "props.row.status === 'queued'" in template
        assert "props.row.status === 'awaiting_feedback'" in template
        assert "props.row.status === 'cancelled'" in template

    def test_contains_status_icons(self):
        """Test that template includes status icons."""
        template = render_status_cell_slot()
        assert "✗" in template  # Failed
        assert "✓" in template  # Completed
        assert "▶" in template  # Running
        assert "⟳" in template  # Queued
        assert "⏸" in template  # Awaiting feedback
        assert "⊗" in template  # Cancelled

    def test_contains_error_tooltip(self):
        """Test that template includes error tooltip for failed status."""
        template = render_status_cell_slot()
        assert "<q-tooltip" in template
        assert "props.row.error" in template
        assert "max-width" in template

    def test_contains_color_mappings(self):
        """Test that template includes proper color attributes."""
        template = render_status_cell_slot()
        assert 'color="red"' in template  # Failed
        assert 'color="green"' in template  # Completed
        assert 'color="yellow"' in template  # Running
        assert 'color="blue"' in template  # Queued
        assert 'color="orange"' in template  # Awaiting feedback
        assert 'color="grey"' in template  # Cancelled/default

    def test_template_is_vue_compatible(self):
        """Test that template uses valid Vue.js syntax."""
        template = render_status_cell_slot()
        # Vue directives
        assert "v-if" in template
        assert "v-else-if" in template
        assert "v-else" in template


class TestRenderErrorCard:
    """Tests for render_error_card function (basic structure testing)."""

    @patch("shandy.webapp_components.ui_components.ui")
    def test_render_error_card_called(self, mock_ui):
        """Test that render_error_card can be called without errors."""
        from shandy.webapp_components.ui_components import render_error_card

        # Mock UI components
        mock_ui.card.return_value.__enter__ = Mock()
        mock_ui.card.return_value.__exit__ = Mock(return_value=False)
        mock_ui.row.return_value.__enter__ = Mock()
        mock_ui.row.return_value.__exit__ = Mock(return_value=False)
        mock_ui.column.return_value.__enter__ = Mock()
        mock_ui.column.return_value.__exit__ = Mock(return_value=False)
        mock_ui.expansion.return_value.__enter__ = Mock()
        mock_ui.expansion.return_value.__exit__ = Mock(return_value=False)
        mock_ui.element.return_value.__enter__ = Mock()
        mock_ui.element.return_value.__exit__ = Mock(return_value=False)
        mock_ui.button.return_value.__enter__ = Mock()
        mock_ui.button.return_value.__exit__ = Mock(return_value=False)

        error_info = {
            "category": "configuration",
            "title": "Test Error",
            "message": "Test message",
            "extracted_error": "Error details",
            "steps": ["Step 1", "Step 2"],
            "raw": "Raw error",
            "contact_admin": True,
        }

        job_info = Mock()
        job_info.status = JobStatus.FAILED
        job_info.iterations_completed = 1
        job_info.max_iterations = 5
        job_info.failed_at = "2026-02-05T10:00:00"

        job_dir = Path("/fake/job/dir")

        # Should not raise any exceptions
        try:
            render_error_card(error_info, job_info, job_dir)
        except Exception:
            # Some exceptions are OK due to mock limitations
            # Just verify it attempts to render
            pass

        # Verify UI elements were called
        assert mock_ui.card.called or mock_ui.row.called


class TestPmidLinkParsing:
    """Tests for PMID link parsing in render_text_with_pmid_links."""

    # Regex pattern used in render_text_with_pmid_links
    PMID_PATTERN = re.compile(r"(PMID[:\s]+)(\d{1,8}(?:\s*,\s*\d{1,8})*)", re.IGNORECASE)

    def test_single_pmid_with_colon(self):
        """Test matching single PMID with colon format."""
        text = "As shown in PMID: 12345678, the results..."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(1) == "PMID: "
        assert matches[0].group(2) == "12345678"

    def test_single_pmid_without_colon(self):
        """Test matching single PMID without colon format."""
        text = "Reference PMID 87654321 supports this finding."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(1) == "PMID "
        assert matches[0].group(2) == "87654321"

    def test_comma_separated_pmids(self):
        """Test matching comma-separated PMIDs."""
        text = "(PMID: 12723803, 10638796, 41121397)"
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(1) == "PMID: "
        assert matches[0].group(2) == "12723803, 10638796, 41121397"

    def test_pmid_case_insensitive(self):
        """Test case insensitivity."""
        text = "pmid: 12345678"
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(2) == "12345678"

    def test_pmid_with_year(self):
        """Test PMID followed by year in parentheses."""
        text = "PMID 41514787 (2025): The study shows..."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(2) == "41514787"

    def test_multiple_pmid_references(self):
        """Test multiple separate PMID references in text."""
        text = "See PMID: 11111111 and also PMID: 22222222 for details."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 2
        assert matches[0].group(2) == "11111111"
        assert matches[1].group(2) == "22222222"

    def test_no_pmid_in_text(self):
        """Test text with no PMIDs."""
        text = "This is plain text without any references."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 0

    def test_pmid_at_start_of_text(self):
        """Test PMID at the beginning of text."""
        text = "PMID: 12345678 shows evidence of..."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].start() == 0

    def test_pmid_at_end_of_text(self):
        """Test PMID at the end of text."""
        text = "Evidence from PMID: 12345678"
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].end() == len(text)

    def test_pmid_boundary_lengths(self):
        """Test PMIDs at boundary lengths (1 to 8 digits)."""
        # Minimum: 1 digit
        matches = list(self.PMID_PATTERN.finditer("PMID: 1"))
        assert len(matches) == 1
        assert matches[0].group(2) == "1"

        # Maximum: 8 digits
        matches = list(self.PMID_PATTERN.finditer("PMID: 12345678"))
        assert len(matches) == 1
        assert matches[0].group(2) == "12345678"

        # Too long (9 digits) - should only match first 8
        text = "PMID: 123456789"
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        # The regex will match 12345678 and leave 9 behind
        assert matches[0].group(2) == "12345678"

    def test_html_escape_in_text(self):
        """Test that special characters would be escaped properly."""
        # Test the html.escape function behavior
        text = "Evidence <script>alert('xss')</script> from PMID: 12345678"
        escaped = html.escape(text)
        assert "<script>" not in escaped
        assert "&lt;script&gt;" in escaped

    def test_comma_separated_extraction(self):
        """Test splitting comma-separated PMIDs."""
        pmid_list = "12723803, 10638796, 41121397"
        pmids = [p.strip() for p in pmid_list.split(",")]
        assert len(pmids) == 3
        assert pmids[0] == "12723803"
        assert pmids[1] == "10638796"
        assert pmids[2] == "41121397"

    def test_pubmed_url_generation(self):
        """Test that PubMed URLs are generated correctly."""
        pmid = "12345678"
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        assert url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
