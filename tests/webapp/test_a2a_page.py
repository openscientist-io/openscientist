"""Exercise the A2A control through NiceGUI with real users and settings."""

from datetime import UTC, datetime, timedelta

import pytest
from nicegui import ui
from nicegui.testing import user_simulation
from sqlalchemy import delete

from openscientist.auth import middleware
from openscientist.database.models import A2ASettings, Administrator, Session
from openscientist.webapp_components.pages.a2a import a2a_page
from tests.api.test_a2a import Environment

pytest_plugins = ["tests.api.test_a2a"]


@pytest.mark.parametrize("user_index", [0, 1])
async def test_a2a_settings_page(
    environment: Environment, monkeypatch: pytest.MonkeyPatch, user_index: int
) -> None:
    env = environment
    monkeypatch.setattr(middleware, "get_admin_session", env.admin_session)
    async with env.admin_session() as session:
        login = Session(
            user_id=env.users[user_index].id, expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
        session.add(login)
        await session.commit()

    async def page() -> None:
        await a2a_page()

    async with user_simulation(root=page) as browser:
        browser.http_client.cookies.set("session_token", str(login.id))
        await browser.open("/")
        await browser.should_see("A2A running")
        await browser.should_see("https://example.org/scientist/a2a")
        if user_index == 1:
            await browser.should_see("Only administrators can turn A2A on or off.")
            await browser.should_not_see("Enable A2A server")
            return

        browser.find(ui.switch).click()
        await browser.should_see("A2A off")
        async with env.factory() as session:
            saved = await session.get(A2ASettings, 1)
            assert saved is not None and not saved.enabled
        await browser.open("/")
        await browser.should_see("A2A off")
        browser.find(ui.switch).click()
        await browser.should_see("A2A running")

        # Removing admin rights while the page is open must revoke this control.
        async with env.factory() as session:
            await session.execute(
                delete(Administrator).where(Administrator.user_id == env.users[0].id)
            )
            await session.commit()
        browser.find(ui.switch).click()
        await browser.should_see("Could not change A2A settings. Administrator access is required.")
        async with env.factory() as session:
            saved = await session.get(A2ASettings, 1)
            assert saved is not None and saved.enabled
