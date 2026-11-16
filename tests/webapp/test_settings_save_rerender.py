"""The admin Settings tab stays on screen after a save."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.testing import user_simulation
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, AdminSettingChange, Session, User
from openscientist.webapp_components.pages.admin import admin_page
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.db_outage import Forwarder


@pytest_asyncio.fixture
async def admin_token(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[str]:
    """A signed-in administrator's session token."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(email=f"admin-{uuid4().hex}@settings.test", name="Admin", is_approved=True)
        db.add(admin)
        await db.flush()
        login, token = Session.issue(
            user_id=admin.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add_all([Administrator(user_id=admin.id), login])
        await db.commit()
    await set_admin_settings_row(test_engine)
    yield token
    await reset_admin_settings_row(test_engine)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(AdminSettingChange))
        await db.execute(delete(User).where(User.id == admin.id))
        await db.commit()


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
