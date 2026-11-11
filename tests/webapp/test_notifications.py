"""Notification controls follow the server airgap policy."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import User
from openscientist.settings import get_settings
from openscientist.webapp_components.components.notifications import _render_notifications_content
from tests.webapp.conftest import OpenBrowser


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
