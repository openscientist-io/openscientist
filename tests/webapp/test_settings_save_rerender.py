"""The admin Settings tab stays on screen after a save, and only for an admin."""

from __future__ import annotations

import asyncio

import pytest
from nicegui import ui
from nicegui.testing import user_simulation
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, User
from openscientist.webapp_components.pages.admin import admin_page
from openscientist.webapp_components.pages.settings_panel import render_settings_panel


@pytest.mark.asyncio
async def test_the_form_is_still_shown_after_a_save(admin_token: str) -> None:
    async with user_simulation(root=admin_page) as browser:
        browser.http_client.cookies.set("session_token", admin_token)
        await browser.open("/")
        await browser.should_see("Agent CPU", retries=50)
        for cpu in browser.find(kind=ui.number, content="Agent CPU").elements:
            cpu.value = 3
        browser.find("Save").click()
        for _ in range(100):
            if browser.notify.messages:
                break
            await asyncio.sleep(0.1)
        assert browser.notify.messages == ["Settings saved."]
        await browser.should_see("Save", retries=50)
        await browser.should_see("Agent CPU")


@pytest.mark.asyncio
async def test_a_session_that_lost_admin_rights_gets_no_form(
    admin_token: str, test_engine: AsyncEngine
) -> None:
    """The Settings tab renders after the page guard, so rights revoked in
    between leave the tab empty."""
    async with AsyncSession(test_engine) as db:
        test_admins = select(User.id).where(User.email.endswith("@settings.test"))
        await db.execute(delete(Administrator).where(Administrator.user_id.in_(test_admins)))
        await db.commit()
    async with user_simulation(root=render_settings_panel) as browser:
        browser.http_client.cookies.set("session_token", admin_token)
        await browser.open("/")
        await browser.should_see("Your admin session is no longer valid", retries=50)
        await browser.should_not_see("Agent CPU")
