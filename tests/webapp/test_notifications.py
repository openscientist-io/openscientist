"""Notification controls follow the server airgap policy, and a test
notification is confirmed even when the page deletes the dialog meanwhile."""

import gc
from collections.abc import AsyncIterator, Awaitable
from contextlib import AbstractContextManager, nullcontext
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import User
from openscientist.settings import get_settings
from openscientist.webapp_components.components import notifications
from openscientist.webapp_components.components.notifications import _render_notifications_content
from tests.webapp.conftest import OpenBrowser, in_event_context


@pytest_asyncio.fixture
async def notification_user(test_engine: AsyncEngine) -> AsyncIterator[User]:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        user = User(
            email=f"notifications-{uuid4().hex}@example.com",
            name="Notification User",
            ntfy_enabled=True,
            ntfy_topic=f"topic-{uuid4().hex}",
        )
        session.add(user)
        await session.commit()
    yield user
    async with test_engine.begin() as connection:
        await connection.execute(delete(User).where(User.id == user.id))


@pytest.mark.asyncio
@pytest.mark.parametrize("airgap_enabled", [True, False], ids=["airgap", "connected"])
async def test_notification_controls_follow_airgap_policy(
    airgap_enabled: bool,
    monkeypatch: pytest.MonkeyPatch,
    notification_user: User,
    browser_client: OpenBrowser,
) -> None:
    monkeypatch.setattr(get_settings().airgap, "enabled", airgap_enabled)
    client = await browser_client(None)
    with client:
        content = ui.column()

        async def reload_content() -> None:
            await _render_notifications_content(content, str(notification_user.id), reload_content)

        await reload_content()

    elements = list(content.descendants())
    labels = [element.text for element in elements if isinstance(element, ui.label)]
    controls = [
        element for element in elements if isinstance(element, (ui.switch, ui.button, ui.input))
    ]
    if airgap_enabled:
        assert any(
            "unavailable" in label.lower() and "air-gapped" in label.lower() for label in labels
        )
        assert all(not control.enabled for control in controls)
        assert not any(isinstance(element, ui.link) for element in elements)
    else:
        assert not any("unavailable" in label.lower() for label in labels)
        assert any(
            isinstance(control, ui.switch)
            and control.text == "Enable push notifications"
            and control.enabled
            and control.value
            for control in controls
        )
        assert any(
            isinstance(control, ui.button)
            and control.text == "Send Test Notification"
            and control.enabled
            for control in controls
        )


async def _render_into_page(client: Client, user_id: str) -> None:
    """Render the dialog's content into the page, keeping no reference to it."""
    with client:
        content = ui.column()

        async def reload_content() -> None:
            await _render_notifications_content(content, user_id, reload_content)

        await reload_content()


def _click_test_button(client: Client) -> Awaitable[None]:
    """Click Send Test Notification the way NiceGUI's socket handler does,
    returning the handler's task without keeping the button alive."""
    button = next(
        element
        for element in client.elements.values()
        if isinstance(element, ui.button) and element.text == "Send Test Notification"
    )
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    scheduled: list[Awaitable[None]] = []

    def _capture(
        awaitable: Awaitable[None],
        *,
        name: str = "",
        context: AbstractContextManager[object] | None = None,
    ) -> None:
        scheduled.append(in_event_context(awaitable, context))

    with pytest.MonkeyPatch.context() as capture:
        capture.setattr(background_tasks, "create_or_defer", _capture)
        with client, button.parent_slot or nullcontext():
            listener.handler(None)
    assert len(scheduled) == 1
    return scheduled[0]


@pytest.mark.asyncio
async def test_a_test_notification_is_confirmed_although_the_page_redraws(
    monkeypatch: pytest.MonkeyPatch,
    notification_user: User,
    browser_client: OpenBrowser,
) -> None:
    """The job page's poll clears the page, the dialog included, while the
    test notification is sent, and the user still learns it was sent."""
    monkeypatch.setattr(get_settings().airgap, "enabled", False)
    client = await browser_client(None)
    await _render_into_page(client, str(notification_user.id))

    async def redrawn_send(**_message: object) -> bool:
        client.content.clear()
        gc.collect()
        return True

    monkeypatch.setattr(notifications, "send_notification", redrawn_send)

    await _click_test_button(client)

    notifies = [data for _target, kind, data in client.outbox.messages if kind == "notify"]
    assert any(data.get("message") == "Test sent!" for data in notifies)
