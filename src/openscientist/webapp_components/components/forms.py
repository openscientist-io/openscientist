"""
Form UI components for OpenScientist web interface.

Provides reusable form-adjacent controls, such as standard dialog action
button rows, used across dialogs throughout the application.
"""

from collections.abc import Awaitable, Callable

from nicegui import ui


def render_dialog_actions(
    on_confirm: Callable[[], None | Awaitable[None]],
    on_cancel: Callable[[], object],
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    confirm_props: str = "color=primary",
) -> None:
    """
    Render standard dialog action buttons (Cancel/Confirm).

    Creates a right-aligned row with Cancel and Confirm buttons.
    Use this in dialogs to ensure consistent footer styling.

    Args:
        on_confirm: Callback when confirm button is clicked (can be async)
        on_cancel: Callback when cancel button is clicked
        confirm_label: Text for the confirm button
        cancel_label: Text for the cancel button
        confirm_props: Quasar props for the confirm button
    """
    with ui.row().classes("w-full justify-end gap-2 mt-4"):
        ui.button(cancel_label, on_click=on_cancel).props("flat")
        ui.button(confirm_label, on_click=on_confirm).props(confirm_props)
