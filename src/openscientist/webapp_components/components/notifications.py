"""Push notification settings dialog for jobs."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from nicegui import ui
from sqlalchemy import select, update

from openscientist.database.models import User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session_ctx
from openscientist.ntfy import ensure_user_has_topic, get_subscription_url, send_notification
from openscientist.settings import get_settings
from openscientist.webapp_components.components.badges import render_job_id_badge

logger = logging.getLogger(__name__)


async def _fetch_notification_settings(user_id: str) -> tuple[bool, str | None] | None:
    """Fetch current ntfy settings for user."""
    async with get_session_ctx() as session:
        await set_current_user(session, UUID(user_id))
        result = await session.execute(
            select(User.ntfy_enabled, User.ntfy_topic).where(User.id == UUID(user_id))
        )
        row = result.first()
    if row is None:
        return None
    return row.ntfy_enabled, row.ntfy_topic


async def _set_notifications_enabled(user_id: str, enabled: bool) -> None:
    """Persist ntfy_enabled flag."""
    async with get_session_ctx() as session:
        await set_current_user(session, UUID(user_id))
        stmt = update(User).where(User.id == UUID(user_id)).values(ntfy_enabled=enabled)
        await session.execute(stmt)
        await session.commit()


def _render_ntfy_topic_details(ntfy_topic: str) -> None:
    """Render subscription links and copy controls for ntfy topic."""
    subscription_url = get_subscription_url(ntfy_topic)
    ui.separator().classes("my-2")
    ui.label("Subscribe to your notifications").classes("text-subtitle2 font-bold mb-2")

    with ui.column().classes("gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("computer", size="sm").classes("text-blue-500")
            ui.link("Open in browser", target=subscription_url, new_tab=True).classes(
                "text-blue-600 underline"
            )
        with ui.row().classes("items-center gap-2"):
            ui.icon("phone_android", size="sm").classes("text-green-500")
            ui.markdown(
                f"**Mobile**: Install [ntfy app](https://ntfy.sh/app) and subscribe to `{ntfy_topic}`"
            )

        def copy_topic() -> None:
            ui.run_javascript(f'navigator.clipboard.writeText("{ntfy_topic}")')
            ui.notify("Copied!", type="positive")

        with ui.row().classes("items-center gap-2 mt-2"):
            topic_input = ui.input(value=ntfy_topic, label="Your topic").classes("flex-grow")
            topic_input.props("readonly outlined dense")
            ui.button(icon="content_copy", on_click=copy_topic).props("flat round").tooltip(
                "Copy topic"
            )


def _render_ntfy_test_button(ntfy_topic: str) -> None:
    """Render button to send a test ntfy notification."""
    ui.separator().classes("my-2")

    async def send_test() -> None:
        success = await send_notification(
            topic=ntfy_topic,
            title="OpenScientist Test",
            message="Test notification - if you see this, it works!",
            tags=["white_check_mark"],
        )
        ui.notify(
            "Test sent!" if success else "Failed to send",
            type="positive" if success else "negative",
        )

    ui.button(
        "Send Test Notification",
        on_click=send_test,
        icon="notifications_active",
    ).props("color=primary")


async def _render_notifications_content(
    content_container: ui.element,
    user_id: str,
    reload_content: Callable[[], Awaitable[None]],
) -> None:
    """Fetch and render notifications content inside dialog."""
    content_container.clear()
    if get_settings().airgap.enabled:
        with content_container:
            ui.label("Push notifications are unavailable in air-gapped mode.").classes(
                "text-gray-500"
            )
        return
    try:
        settings = await _fetch_notification_settings(user_id)
        if settings is None:
            with content_container:
                ui.label("User not found").classes("text-red-500")
            return
        ntfy_enabled, ntfy_topic = settings
        if ntfy_enabled and not ntfy_topic:
            ntfy_topic = await ensure_user_has_topic(UUID(user_id))
    except Exception as exc:
        logger.error("Error loading settings: %s", exc, exc_info=True)
        with content_container:
            ui.label("Error loading settings. Check server logs.").classes("text-red-500")
        return

    with content_container:

        async def toggle_notifications(event: Any) -> None:
            new_value = event.value
            try:
                await _set_notifications_enabled(user_id, new_value)
                ui.notify(
                    "Notifications enabled" if new_value else "Notifications disabled",
                    type="positive",
                )
                await reload_content()
            except Exception as exc:
                logger.error("Failed to toggle notifications: %s", exc, exc_info=True)
                ui.notify(f"Failed to update: {exc}", type="negative")

        ui.switch(
            "Enable push notifications",
            value=ntfy_enabled,
            on_change=toggle_notifications,
        ).classes("mb-4")

        if not ntfy_enabled:
            ui.label(
                "Enable notifications to receive alerts when jobs complete, fail, or need feedback."
            ).classes("text-gray-500")
            return
        if not ntfy_topic:
            ui.label("Setting up your notification topic...").classes("text-gray-500")
            return
        _render_ntfy_topic_details(ntfy_topic)
        _render_ntfy_test_button(ntfy_topic)


def render_notifications_dialog(job_id: str, user_id: str | None = None) -> ui.dialog:
    """Create and return a notifications dialog for a job."""
    with ui.dialog() as dialog, ui.card().classes("w-[500px]"):
        with ui.row().classes("items-center gap-2 mb-4"):
            ui.label("Push Notifications").classes("text-h6")
            if job_id and job_id != "notifications" and len(job_id) > 10:
                render_job_id_badge(job_id)

        if not user_id:
            ui.label("Not logged in").classes("text-red-500")
            content_container = None
        else:
            content_container = ui.column().classes("w-full")
            with content_container:
                ui.label("Loading...").classes("text-gray-500")

        with ui.row().classes("w-full justify-end gap-2 mt-4"):
            ui.button("Close", on_click=dialog.close)

        if content_container and user_id:

            async def load_content() -> None:
                await _render_notifications_content(content_container, user_id, load_content)

            dialog.on("show", load_content)

    return dialog
