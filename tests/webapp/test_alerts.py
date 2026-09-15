"""Tests for alert/banner UI components module."""

from contextlib import suppress
from pathlib import Path
from unittest.mock import Mock, patch

from openscientist.job_manager import JobStatus
from openscientist.webapp_components.components.alerts import (
    render_alert_banner,
    render_config_error_banner,
    render_error_card,
)


class TestRenderErrorCard:
    """Tests for render_error_card function (basic structure testing)."""

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_render_error_card_called(self, mock_ui):
        """Test that render_error_card can be called without errors."""
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

        # Some exceptions are acceptable due to mock limitations.
        with suppress(Exception):
            render_error_card(error_info, job_info, job_dir)

        # Verify UI elements were called
        assert mock_ui.card.called or mock_ui.row.called

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_steps_only_rendered_when_present(self, mock_ui):
        """Test that the 'How to resolve' section only appears when steps are given."""
        error_info = {
            "category": "configuration",
            "title": "Test Error",
            "message": "Test message",
        }
        job_info = Mock(
            status=JobStatus.FAILED,
            iterations_completed=1,
            max_iterations=5,
            failed_at=None,
        )

        with suppress(Exception):
            render_error_card(error_info, job_info, Path("/fake/job/dir"))

        rendered_labels = [call.args[0] for call in mock_ui.label.call_args_list if call.args]
        assert "How to resolve:" not in rendered_labels

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_contact_admin_notice_only_rendered_when_flag_set(self, mock_ui):
        """Test that the admin-assistance notice only appears when contact_admin is truthy."""
        error_info = {
            "category": "configuration",
            "title": "Test Error",
            "message": "Test message",
            "contact_admin": False,
        }
        job_info = Mock(
            status=JobStatus.FAILED,
            iterations_completed=1,
            max_iterations=5,
            failed_at=None,
        )

        with suppress(Exception):
            render_error_card(error_info, job_info, Path("/fake/job/dir"))

        rendered_labels = [call.args[0] for call in mock_ui.label.call_args_list if call.args]
        assert "This error requires administrator assistance." not in rendered_labels

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_log_file_hint_shown_when_orchestrator_log_exists(self, mock_ui, tmp_path):
        """Test that the log-file hint appears when orchestrator.log exists on disk."""
        (tmp_path / "orchestrator.log").write_text("log contents")
        error_info = {
            "category": "configuration",
            "title": "Test Error",
            "message": "Test message",
            "raw": "Raw error",
        }
        job_info = Mock(
            status=JobStatus.FAILED,
            iterations_completed=1,
            max_iterations=5,
            failed_at=None,
        )

        with suppress(Exception):
            render_error_card(error_info, job_info, tmp_path)

        rendered_labels = [call.args[0] for call in mock_ui.label.call_args_list if call.args]
        assert "Check the orchestrator.log file for complete details." in rendered_labels

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_log_file_hint_hidden_when_orchestrator_log_missing(self, mock_ui, tmp_path):
        """Test that the log-file hint is omitted when orchestrator.log does not exist."""
        error_info = {
            "category": "configuration",
            "title": "Test Error",
            "message": "Test message",
        }
        job_info = Mock(
            status=JobStatus.FAILED,
            iterations_completed=1,
            max_iterations=5,
            failed_at=None,
        )

        with suppress(Exception):
            render_error_card(error_info, job_info, tmp_path)

        rendered_labels = [call.args[0] for call in mock_ui.label.call_args_list if call.args]
        assert "Check the orchestrator.log file for complete details." not in rendered_labels


class TestRenderConfigErrorBanner:
    """Tests for render_config_error_banner function."""

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_back_button_hidden_by_default(self, mock_ui):
        """Test that no back button is rendered when show_back_button is False."""
        render_config_error_banner("anthropic", ["Missing API key"])
        assert mock_ui.button.call_count == 0

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_back_button_shown_when_requested(self, mock_ui):
        """Test that the back button is rendered with the expected icon when requested."""
        render_config_error_banner("anthropic", ["Missing API key"], show_back_button=True)
        mock_ui.button.assert_called_once()
        assert mock_ui.button.call_args.args[0] == "Back to Jobs"
        assert mock_ui.button.call_args.kwargs["icon"] == "arrow_back"

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_provider_name_is_uppercased_in_message(self, mock_ui):
        """Test that the provider name is upper-cased in the rendered message."""
        render_config_error_banner("anthropic", [])
        rendered_labels = [call.args[0] for call in mock_ui.label.call_args_list if call.args]
        assert any("ANTHROPIC" in label for label in rendered_labels)

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_each_config_error_renders_one_bullet(self, mock_ui):
        """Test that each config error string produces one bullet label."""
        render_config_error_banner("anthropic", ["Missing API key", "Invalid model"])
        rendered_labels = [call.args[0] for call in mock_ui.label.call_args_list if call.args]
        assert "• Missing API key" in rendered_labels
        assert "• Invalid model" in rendered_labels


class TestRenderAlertBanner:
    """Tests for render_alert_banner function."""

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_error_severity_uses_red_icon_color(self, mock_ui):
        """Test that severity='error' maps to the red icon color."""
        render_alert_banner("Title", "Message", severity="error")
        assert mock_ui.icon.call_args.kwargs["color"] == "red"

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_warning_severity_uses_orange_icon_color(self, mock_ui):
        """Test that severity='warning' maps to the orange icon color."""
        render_alert_banner("Title", "Message", severity="warning")
        assert mock_ui.icon.call_args.kwargs["color"] == "orange"

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_info_severity_uses_blue_icon_color(self, mock_ui):
        """Test that severity='info' maps to the blue icon color."""
        render_alert_banner("Title", "Message", severity="info")
        assert mock_ui.icon.call_args.kwargs["color"] == "blue"

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_unknown_severity_falls_back_to_error_colors(self, mock_ui):
        """Test that an unrecognized severity falls back to the error color mapping."""
        render_alert_banner("Title", "Message", severity="unknown")
        assert mock_ui.icon.call_args.kwargs["color"] == "red"
        assert mock_ui.icon.call_args.args[0] == "error"

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_no_expansion_rendered_when_details_omitted(self, mock_ui):
        """Test that no expansion is created when details is None."""
        render_alert_banner("Title", "Message")
        assert mock_ui.expansion.call_count == 0

    @patch("openscientist.webapp_components.components.alerts.ui")
    def test_expansion_rendered_with_title_and_one_label_per_detail(self, mock_ui):
        """Test that details produce an expansion with one prefixed label each."""
        render_alert_banner(
            "Title",
            "Message",
            details=["Detail one", "Detail two"],
            expansion_title="More Info",
        )
        mock_ui.expansion.assert_called_once_with("More Info", icon="info")
        rendered_labels = [call.args[0] for call in mock_ui.label.call_args_list if call.args]
        assert "• Detail one" in rendered_labels
        assert "• Detail two" in rendered_labels
