"""
Action button UI components for OpenScientist web interface.

Provides reusable action-button rows (share, delete, notifications, etc.)
with consistent styling across the application.
"""

from collections.abc import Callable

from nicegui import ui


def render_job_action_buttons(
    on_share: Callable[[], None] | None = None,
    on_delete: Callable[[], None] | None = None,
    on_notifications: Callable[[], None] | None = None,
) -> None:
    """
    Render job action buttons (share, delete, notifications) in the same style as table actions.

    Uses round, flat, dense icon buttons with tooltips - same visual style as
    the table action column buttons from render_actions_slot_with_delete().

    Args:
        on_share: Callback for share button click. If None, share button is hidden.
        on_delete: Callback for delete button click. If None, delete button is hidden.
        on_notifications: Callback for notifications button click. If None, button is hidden.
    """
    with ui.row().classes("gap-1 items-center"):
        if on_notifications:
            with ui.button(icon="notifications", on_click=on_notifications).props(
                "round flat dense size=sm color=primary"
            ):
                ui.tooltip("Configure push notifications")

        if on_share:
            with ui.button(icon="share", on_click=on_share).props(
                "round flat dense size=sm color=primary"
            ):
                ui.tooltip("Share job")

        if on_delete:
            with ui.button(icon="delete", on_click=on_delete).props(
                "round flat dense size=sm color=negative"
            ):
                ui.tooltip("Delete job")
