"""The /admin page built in a real signed-in NiceGUI client, read at the
moment NiceGUI sends it (see ``served_page``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, ReviewToken, Session, User
from openscientist.webapp_components.pages.admin import admin_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenPage, ServedPage, fire_click_twice


@pytest_asyncio.fixture
async def admin_with_a_pending_user(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An admin's live session and an unapproved user, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(email=f"admin-{uuid4().hex}@admin.test", name="Admin", is_approved=True)
        pending = User(email=f"pending-{uuid4().hex}@admin.test", name="Pending", is_approved=False)
        db.add_all([admin, pending])
        await db.flush()
        db.add(Administrator(user_id=admin.id))
        login = Session(user_id=admin.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {"token": str(login.id), "pending_id": str(pending.id)}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id.in_([admin.id, pending.id])))
        await db.commit()


@pytest_asyncio.fixture
async def signed_in_admin(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, str]]:
    """An admin's live session, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(
            email=f"double-token-{uuid4().hex}@admin.test",
            name="Token Admin",
            is_approved=True,
        )
        db.add(admin)
        await db.flush()
        db.add(Administrator(user_id=admin.id))
        login = Session(user_id=admin.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield admin, str(login.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == admin.id))
        await db.commit()


def _user_row_ids(client: Client) -> set[str]:
    tables = [e for e in client.elements.values() if isinstance(e, ui.table)]
    return {str(row["id"]) for table in tables for row in table.rows}


@pytest.mark.asyncio
async def test_the_first_html_already_holds_the_users_table(
    database: Forwarder, admin_with_a_pending_user: dict[str, str], served_page: ServedPage
) -> None:
    """The Users tab, shown first, is filled before NiceGUI sends the page."""
    _, sent = await served_page(admin_with_a_pending_user["token"], admin_page, _user_row_ids)
    assert admin_with_a_pending_user["pending_id"] in sent


@pytest.mark.asyncio
async def test_a_double_click_creates_exactly_one_review_token(
    database: Forwarder,
    test_engine: AsyncEngine,
    signed_in_admin: tuple[User, str],
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clicks racing on the event loop commit exactly one review token
    and re-enable the Create button when the first call finishes."""
    from openscientist.webapp_components.pages import admin as admin_module

    admin, token = signed_in_admin
    label = f"Double click {uuid4().hex}"

    async def build() -> None:
        await admin_module.render_review_tokens_panel()

    client = await signed_in_page(token, build)
    label_input = next(
        e
        for e in client.layout.descendants()
        if isinstance(e, ui.input) and e.label == "Reviewer Label"
    )
    label_input.value = label
    button = next(
        e for e in client.layout.descendants() if isinstance(e, ui.button) and e.text == "Create"
    )

    await fire_click_twice(button, client, monkeypatch)

    async with AsyncSession(test_engine) as db:
        rows = (
            (await db.execute(select(ReviewToken).where(ReviewToken.label == label)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].created_by_id == admin.id
    assert button.enabled is True
    dialogs = [e for e in client.layout.descendants() if isinstance(e, ui.dialog)]
    assert len(dialogs) == 1
    assert dialogs[0]._props.get("persistent") is True
