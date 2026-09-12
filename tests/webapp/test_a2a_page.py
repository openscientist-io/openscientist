"""Exercise the A2A control through NiceGUI with real users and settings."""

from datetime import UTC, datetime, timedelta

import pytest
from nicegui import ui
from nicegui.testing import user_simulation
from sqlalchemy import delete

from openscientist.api import a2a
from openscientist.auth import middleware
from openscientist.database.models import A2ASettings, Administrator, Session
from openscientist.webapp_components.pages import a2a as a2a_page_module
from openscientist.webapp_components.pages.a2a import a2a_page
from tests.a2a_fixtures import Environment


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
        await browser.should_see("Try it with the Go client")
        await browser.should_see('a2a send "$OPENSCIENTIST_URL"')
        if user_index == 1:
            await browser.should_see("Only administrators can turn A2A on or off.")
            await browser.should_not_see("Enable A2A server")
            return

        # Simulate the browser's model event; click() only assigns the Python value.
        browser.find(ui.switch).trigger("update:modelValue", False)
        await browser.should_see("A2A off")
        async with env.factory() as session:
            saved = await session.get(A2ASettings, 1)
            assert saved is not None and not saved.enabled
        await browser.open("/")
        await browser.should_see("A2A off")
        browser.find(ui.switch).trigger("update:modelValue", True)
        await browser.should_see("A2A running")

        # Removing admin rights while the page is open must revoke this control.
        async with env.factory() as session:
            await session.execute(
                delete(Administrator).where(Administrator.user_id == env.users[0].id)
            )
            await session.commit()
        browser.find(ui.switch).trigger("update:modelValue", False)
        await browser.should_see("Could not change A2A settings. Administrator access is required.")
        async with env.factory() as session:
            saved = await session.get(A2ASettings, 1)
            assert saved is not None and saved.enabled


async def test_settings_unavailable_then_recovers(
    environment: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = environment
    monkeypatch.setattr(middleware, "get_admin_session", env.admin_session)
    async with env.admin_session() as session:
        login = Session(user_id=env.users[0].id, expires_at=datetime.now(UTC) + timedelta(hours=1))
        session.add(login)
        await session.commit()

    real_status = a2a.service_status
    unavailable = True

    async def flaky_status() -> dict[str, object]:
        if unavailable:
            raise ConnectionError("Database connection lost")
        return await real_status()

    monkeypatch.setattr(a2a, "service_status", flaky_status)
    monkeypatch.setattr(a2a_page_module, "service_status", flaky_status)

    async def page() -> None:
        await a2a_page()

    async with user_simulation(root=page) as browser:
        browser.http_client.cookies.set("session_token", str(login.id))
        await browser.open("/")
        await browser.should_see("A2A unavailable")
        await browser.should_see("Could not confirm A2A status")
        assert all(not switch.enabled for switch in browser.find(ui.switch).elements)
        unavailable = False
        await browser.should_see("A2A running", retries=65)
        await browser.should_not_see("Could not confirm A2A status")
        assert all(switch.enabled for switch in browser.find(ui.switch).elements)
