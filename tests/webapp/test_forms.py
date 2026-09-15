"""Tests for form UI components module."""

from unittest.mock import Mock, patch

from openscientist.webapp_components.components.forms import render_dialog_actions


class TestRenderDialogActionsLabels:
    """Tests for button label behavior."""

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_default_labels_are_confirm_and_cancel(self, mock_ui):
        """Test that default labels are used when not overridden."""
        render_dialog_actions(on_confirm=Mock(), on_cancel=Mock())

        call_labels = [call.args[0] for call in mock_ui.button.call_args_list]
        assert call_labels == ["Cancel", "Confirm"]

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_custom_labels_are_used(self, mock_ui):
        """Test that custom confirm/cancel labels are passed through."""
        render_dialog_actions(
            on_confirm=Mock(),
            on_cancel=Mock(),
            confirm_label="Delete",
            cancel_label="Nevermind",
        )

        call_labels = [call.args[0] for call in mock_ui.button.call_args_list]
        assert call_labels == ["Nevermind", "Delete"]


class TestRenderDialogActionsCallbacks:
    """Tests for callback wiring."""

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_confirm_button_wired_to_on_confirm(self, mock_ui):
        """Test that the confirm button's on_click is the on_confirm callable."""
        on_confirm = Mock()
        render_dialog_actions(on_confirm=on_confirm, on_cancel=Mock())

        confirm_call = mock_ui.button.call_args_list[1]
        assert confirm_call.kwargs["on_click"] is on_confirm

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_cancel_button_wired_to_on_cancel(self, mock_ui):
        """Test that the cancel button's on_click is the on_cancel callable."""
        on_cancel = Mock()
        render_dialog_actions(on_confirm=Mock(), on_cancel=on_cancel)

        cancel_call = mock_ui.button.call_args_list[0]
        assert cancel_call.kwargs["on_click"] is on_cancel

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_on_confirm_supports_async_callable(self, mock_ui):
        """Test that an async on_confirm callable is wired without error."""

        async def async_confirm() -> None:
            pass

        render_dialog_actions(on_confirm=async_confirm, on_cancel=Mock())

        confirm_call = mock_ui.button.call_args_list[1]
        assert confirm_call.kwargs["on_click"] is async_confirm

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_cancel_click_does_not_touch_confirm_callback(self, mock_ui):
        """Test that confirm and cancel callbacks are wired independently."""
        on_confirm = Mock()
        on_cancel = Mock()
        render_dialog_actions(on_confirm=on_confirm, on_cancel=on_cancel)

        cancel_call = mock_ui.button.call_args_list[0]
        confirm_call = mock_ui.button.call_args_list[1]
        assert cancel_call.kwargs["on_click"] is on_cancel
        assert cancel_call.kwargs["on_click"] is not on_confirm
        assert confirm_call.kwargs["on_click"] is on_confirm
        assert confirm_call.kwargs["on_click"] is not on_cancel


class TestRenderDialogActionsProps:
    """Tests for Quasar props applied to each button."""

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_default_confirm_props_is_color_primary(self, mock_ui):
        """Test that the confirm button defaults to color=primary."""
        render_dialog_actions(on_confirm=Mock(), on_cancel=Mock())

        props_calls = mock_ui.button.return_value.props.call_args_list
        assert props_calls[1].args[0] == "color=primary"

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_custom_confirm_props_applied(self, mock_ui):
        """Test that a custom confirm_props value overrides the default."""
        render_dialog_actions(on_confirm=Mock(), on_cancel=Mock(), confirm_props="color=negative")

        props_calls = mock_ui.button.return_value.props.call_args_list
        assert props_calls[1].args[0] == "color=negative"

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_cancel_button_always_uses_flat_props(self, mock_ui):
        """Test that the cancel button always receives flat props, regardless of confirm_props."""
        render_dialog_actions(on_confirm=Mock(), on_cancel=Mock(), confirm_props="color=negative")

        props_calls = mock_ui.button.return_value.props.call_args_list
        assert props_calls[0].args[0] == "flat"


class TestRenderDialogActionsLayout:
    """Tests for layout and button count."""

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_two_buttons_rendered(self, mock_ui):
        """Test that exactly two buttons are created."""
        render_dialog_actions(on_confirm=Mock(), on_cancel=Mock())
        assert mock_ui.button.call_count == 2

    @patch("openscientist.webapp_components.components.forms.ui")
    def test_row_uses_right_aligned_classes(self, mock_ui):
        """Test that the button row uses the expected right-aligned layout classes."""
        render_dialog_actions(on_confirm=Mock(), on_cancel=Mock())

        mock_ui.row.return_value.classes.assert_called_once_with("w-full justify-end gap-2 mt-4")


class TestRenderDialogActionsReexportedFromUiComponents:
    """
    render_dialog_actions was extracted to
    openscientist.webapp_components.components.forms. This test guards the
    backward-compatibility re-export from ui_components, since production code
    across the app still imports it directly from this module.
    """

    def test_render_dialog_actions_is_same_object_in_both_modules(self):
        """ui_components must expose the exact same render_dialog_actions object."""
        from openscientist.webapp_components import ui_components
        from openscientist.webapp_components.components import forms

        assert ui_components.render_dialog_actions is forms.render_dialog_actions
