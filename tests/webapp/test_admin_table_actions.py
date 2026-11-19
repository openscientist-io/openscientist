"""Confirming a delete or revoke from an admin table keeps the table, with
the remaining rows, on screen."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.testing import User as SimulatedBrowser
from nicegui.testing import user_simulation
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, ReviewToken, Session, User
from openscientist.webapp_components.pages.admin import (
    render_review_tokens_panel,
    render_users_panel,
)
from tests.db_outage import Forwarder


@dataclass(frozen=True)
class AdminWorld:
    """A signed-in administrator, two other users and two of the admin's review tokens."""

    token: str
    doomed_user: str
    kept_user: str
    doomed_review_token: str
    kept_review_token: str


@pytest_asyncio.fixture
async def admin_world(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[AdminWorld]:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(email=f"admin-{uuid4().hex}@tables.test", name="Admin", is_approved=True)
        doomed = User(email=f"doomed-{uuid4().hex}@tables.test", name="Doomed")
        kept = User(email=f"kept-{uuid4().hex}@tables.test", name="Kept")
        db.add_all([admin, doomed, kept])
        await db.flush()
        login, token = Session.issue(
            user_id=admin.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        doomed_review = ReviewToken(
            token_hash=uuid4().hex, label="Doomed reviewer", created_by_id=admin.id
        )
        kept_review = ReviewToken(
            token_hash=uuid4().hex, label="Kept reviewer", created_by_id=admin.id
        )
        db.add_all([Administrator(user_id=admin.id), login, doomed_review, kept_review])
        await db.commit()
    yield AdminWorld(
        token=token,
        doomed_user=str(doomed.id),
        kept_user=str(kept.id),
        doomed_review_token=str(doomed_review.id),
        kept_review_token=str(kept_review.id),
    )
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(ReviewToken).where(ReviewToken.created_by_id == admin.id))
        await db.execute(delete(User).where(User.id.in_([admin.id, doomed.id, kept.id])))
        await db.commit()


def _row_ids(browser: SimulatedBrowser) -> set[str]:
    return {str(row["id"]) for table in browser.find(ui.table).elements for row in table.rows}


@pytest.mark.asyncio
async def test_deleting_a_user_keeps_the_other_users_listed(admin_world: AdminWorld) -> None:
    async with user_simulation(root=render_users_panel) as browser:
        browser.http_client.cookies.set("session_token", admin_world.token)
        await browser.open("/")
        await browser.should_see(kind=ui.table, retries=50)
        browser.find(ui.table).trigger(
            "delete-user", {"id": admin_world.doomed_user, "name": "Doomed"}
        )
        await browser.should_see("Delete User")
        browser.find(kind=ui.button, content="Delete").click()
        await browser.should_see("User deleted successfully", retries=50)
        await browser.should_see(kind=ui.table, retries=50)
        assert admin_world.kept_user in _row_ids(browser)
        assert admin_world.doomed_user not in _row_ids(browser)


@pytest.mark.asyncio
async def test_revoking_a_review_token_keeps_the_other_tokens_listed(
    admin_world: AdminWorld,
) -> None:
    async with user_simulation(root=render_review_tokens_panel) as browser:
        browser.http_client.cookies.set("session_token", admin_world.token)
        await browser.open("/")
        await browser.should_see(kind=ui.table, retries=50)
        browser.find(ui.table).trigger(
            "revoke", {"id": admin_world.doomed_review_token, "label": "Doomed reviewer"}
        )
        await browser.should_see("Revoke Review Token")
        browser.find(kind=ui.button, content="Revoke").click()
        await browser.should_see("Token revoked", retries=50)
        await browser.should_see(kind=ui.table, retries=50)
        assert {admin_world.kept_review_token, admin_world.doomed_review_token} <= _row_ids(browser)
        [doomed] = [
            row
            for table in browser.find(ui.table).elements
            for row in table.rows
            if row["id"] == admin_world.doomed_review_token
        ]
        assert doomed["can_revoke"] is False
