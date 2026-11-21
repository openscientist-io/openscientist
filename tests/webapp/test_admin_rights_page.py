"""Granting and removing admin rights from the admin page's users table."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.testing import User as SimulatedBrowser
from nicegui.testing import user_simulation
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, Session, User
from openscientist.webapp_components.pages.admin import render_users_panel
from tests.db_outage import Forwarder


@dataclass(frozen=True)
class _Staff:
    token: str
    admin: UUID
    bob: UUID
    carol: UUID


@pytest_asyncio.fixture
async def staff(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[_Staff]:
    """A signed-in admin, Bob who is also an admin, and Carol who is not."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(email=f"admin-{uuid4().hex}@rights.test", name="Admin", is_approved=True)
        bob = User(email=f"bob-{uuid4().hex}@rights.test", name="Bob")
        carol = User(email=f"carol-{uuid4().hex}@rights.test", name="Carol")
        db.add_all([admin, bob, carol])
        await db.flush()
        login, token = Session.issue(
            user_id=admin.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add_all([Administrator(user_id=admin.id), Administrator(user_id=bob.id), login])
        await db.commit()
    yield _Staff(token, admin.id, bob.id, carol.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id.in_([admin.id, bob.id, carol.id])))
        await db.commit()


def _row(browser: SimulatedBrowser, user_id: UUID) -> dict[str, Any]:
    [row] = [
        row
        for table in browser.find(ui.table).elements
        for row in table.rows
        if row["id"] == str(user_id)
    ]
    return row


async def _granter(test_engine: AsyncEngine, user_id: UUID) -> list[UUID | None]:
    async with AsyncSession(test_engine) as db:
        rows = await db.execute(
            select(Administrator.granted_by).where(Administrator.user_id == user_id)
        )
        return list(rows.scalars())


@pytest.mark.asyncio
async def test_the_users_table_marks_which_users_are_admins(staff: _Staff) -> None:
    async with user_simulation(root=render_users_panel) as browser:
        browser.http_client.cookies.set("session_token", staff.token)
        await browser.open("/")
        await browser.should_see(kind=ui.table, retries=50)
        assert _row(browser, staff.bob)["is_admin"] is True
        assert _row(browser, staff.carol)["is_admin"] is False


@pytest.mark.asyncio
async def test_an_admin_makes_another_user_an_admin_after_confirming(
    staff: _Staff, test_engine: AsyncEngine
) -> None:
    async with user_simulation(root=render_users_panel) as browser:
        browser.http_client.cookies.set("session_token", staff.token)
        await browser.open("/")
        await browser.should_see(kind=ui.table, retries=50)
        browser.find(ui.table).trigger("grant-admin", {"id": str(staff.carol), "name": "Carol"})
        await browser.should_see('Make "Carol" an admin?')
        assert await _granter(test_engine, staff.carol) == []
        browser.find(kind=ui.button, content="Make admin").click()
        await browser.should_see("User is now an admin", retries=50)
        await browser.should_see(kind=ui.table, retries=50)
        assert _row(browser, staff.carol)["is_admin"] is True
    assert await _granter(test_engine, staff.carol) == [staff.admin]


@pytest.mark.asyncio
async def test_an_admin_removes_another_admins_rights_after_confirming(
    staff: _Staff, test_engine: AsyncEngine
) -> None:
    async with user_simulation(root=render_users_panel) as browser:
        browser.http_client.cookies.set("session_token", staff.token)
        await browser.open("/")
        await browser.should_see(kind=ui.table, retries=50)
        browser.find(ui.table).trigger("revoke-admin", {"id": str(staff.bob), "name": "Bob"})
        await browser.should_see('Remove the admin rights of "Bob"?')
        browser.find(kind=ui.button, content="Remove admin").click()
        await browser.should_see("User is no longer an admin", retries=50)
        await browser.should_see(kind=ui.table, retries=50)
        assert _row(browser, staff.bob)["is_admin"] is False
    assert await _granter(test_engine, staff.bob) == []
