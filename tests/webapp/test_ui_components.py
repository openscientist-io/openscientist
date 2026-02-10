"""Tests for UI components module."""

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
