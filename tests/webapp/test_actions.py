"""Tests for action-button UI components module."""

from unittest.mock import Mock, patch

from openscientist.webapp_components.components.actions import render_job_action_buttons


class TestRenderJobActionButtonsCreation:
    """Tests for which buttons are created based on provided callbacks."""

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_no_buttons_created_when_no_callbacks_provided(self, mock_ui):
        """Test that no buttons are rendered when all callbacks are omitted."""
        render_job_action_buttons()
        assert mock_ui.button.call_count == 0

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_only_share_button_created_when_only_on_share_provided(self, mock_ui):
        """Test that omitting on_delete/on_notifications skips their buttons."""
        render_job_action_buttons(on_share=Mock())
        assert mock_ui.button.call_count == 1
        assert mock_ui.button.call_args.kwargs["icon"] == "share"

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_only_delete_button_created_when_only_on_delete_provided(self, mock_ui):
        """Test that omitting on_share/on_notifications skips their buttons."""
        render_job_action_buttons(on_delete=Mock())
        assert mock_ui.button.call_count == 1
        assert mock_ui.button.call_args.kwargs["icon"] == "delete"

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_only_notifications_button_created_when_only_on_notifications_provided(self, mock_ui):
        """Test that omitting on_share/on_delete skips their buttons."""
        render_job_action_buttons(on_notifications=Mock())
        assert mock_ui.button.call_count == 1
        assert mock_ui.button.call_args.kwargs["icon"] == "notifications"

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_all_three_buttons_created_when_all_callbacks_provided(self, mock_ui):
        """Test that providing all three callbacks renders all three buttons."""
        render_job_action_buttons(on_share=Mock(), on_delete=Mock(), on_notifications=Mock())
        assert mock_ui.button.call_count == 3

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_buttons_are_created_in_notifications_share_delete_order(self, mock_ui):
        """Test that the button creation order matches the current implementation."""
        render_job_action_buttons(on_share=Mock(), on_delete=Mock(), on_notifications=Mock())
        icons_in_order = [call.kwargs["icon"] for call in mock_ui.button.call_args_list]
        assert icons_in_order == ["notifications", "share", "delete"]


class TestRenderJobActionButtonsCallbackBinding:
    """Tests for correct callback-to-button wiring."""

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_share_button_bound_to_on_share_callback(self, mock_ui):
        """Test that the share button's on_click is exactly the on_share callable."""
        on_share = Mock()
        render_job_action_buttons(on_share=on_share)
        assert mock_ui.button.call_args.kwargs["on_click"] is on_share

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_delete_button_bound_to_on_delete_callback(self, mock_ui):
        """Test that the delete button's on_click is exactly the on_delete callable."""
        on_delete = Mock()
        render_job_action_buttons(on_delete=on_delete)
        assert mock_ui.button.call_args.kwargs["on_click"] is on_delete

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_notifications_button_bound_to_on_notifications_callback(self, mock_ui):
        """Test that the notifications button's on_click is exactly the on_notifications callable."""
        on_notifications = Mock()
        render_job_action_buttons(on_notifications=on_notifications)
        assert mock_ui.button.call_args.kwargs["on_click"] is on_notifications

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_each_callback_bound_independently_to_its_own_button(self, mock_ui):
        """Test that share/delete/notifications callbacks aren't cross-wired to each other."""
        on_share = Mock()
        on_delete = Mock()
        on_notifications = Mock()
        render_job_action_buttons(
            on_share=on_share, on_delete=on_delete, on_notifications=on_notifications
        )

        calls_by_icon = {call.kwargs["icon"]: call for call in mock_ui.button.call_args_list}
        assert calls_by_icon["notifications"].kwargs["on_click"] is on_notifications
        assert calls_by_icon["share"].kwargs["on_click"] is on_share
        assert calls_by_icon["delete"].kwargs["on_click"] is on_delete


class TestRenderJobActionButtonsStyling:
    """Tests for icons, tooltips, and Quasar props remaining unchanged."""

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_share_and_notifications_buttons_use_primary_props(self, mock_ui):
        """Test that share and notifications buttons keep the primary round/flat/dense styling."""
        render_job_action_buttons(on_share=Mock(), on_notifications=Mock())

        props_calls = mock_ui.button.return_value.props.call_args_list
        assert all(call.args[0] == "round flat dense size=sm color=primary" for call in props_calls)

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_delete_button_uses_negative_color_props(self, mock_ui):
        """Test that the delete button keeps round/flat/dense styling with negative color."""
        render_job_action_buttons(on_delete=Mock())

        props_call = mock_ui.button.return_value.props.call_args_list[0]
        assert props_call.args[0] == "round flat dense size=sm color=negative"

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_tooltips_match_each_button_in_order(self, mock_ui):
        """Test that tooltip text matches the notifications/share/delete render order."""
        render_job_action_buttons(on_share=Mock(), on_delete=Mock(), on_notifications=Mock())

        tooltip_texts = [call.args[0] for call in mock_ui.tooltip.call_args_list]
        assert tooltip_texts == [
            "Configure push notifications",
            "Share job",
            "Delete job",
        ]


class TestRenderJobActionButtonsLayout:
    """Tests for the row container the buttons render within."""

    @patch("openscientist.webapp_components.components.actions.ui")
    def test_buttons_render_within_a_row_with_expected_classes(self, mock_ui):
        """Test that the buttons are wrapped in a row with the expected layout classes."""
        render_job_action_buttons(on_share=Mock())

        mock_ui.row.assert_called_once()
        mock_ui.row.return_value.classes.assert_called_once_with("gap-1 items-center")
