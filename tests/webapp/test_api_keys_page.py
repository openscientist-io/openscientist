"""The /api-keys page built in a real signed-in NiceGUI client, read at the
moment NiceGUI sends it (see ``served_page``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, APIKey, Session, User
from openscientist.webapp_components.pages.api_keys import api_keys_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage


@pytest_asyncio.fixture
async def key_holders(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An admin and a user, each with one API key and a live session, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(email=f"admin-{uuid4().hex}@keys.test", name="Admin", is_approved=True)
        user = User(email=f"user-{uuid4().hex}@keys.test", name="User", is_approved=True)
        db.add_all([admin, user])
        await db.flush()
        db.add(Administrator(user_id=admin.id))
        admin_key = APIKey(user_id=admin.id, name="admin-script", key_hash=uuid4().hex)
        user_key = APIKey(user_id=user.id, name="user-script", key_hash=uuid4().hex)
        expires = datetime.now(UTC) + timedelta(days=1)
        admin_login = Session(user_id=admin.id, expires_at=expires)
        user_login = Session(user_id=user.id, expires_at=expires)
        db.add_all([admin_key, user_key, admin_login, user_login])
        await db.commit()
    yield {
        "admin_token": str(admin_login.id),
        "user_token": str(user_login.id),
        "admin_key": str(admin_key.id),
        "user_key": str(user_key.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id.in_([admin.id, user.id])))
        await db.commit()


def _key_ids(client: Client) -> set[str]:
    tables = [e for e in client.elements.values() if isinstance(e, ui.table)]
    return {str(row["id"]) for table in tables for row in table.rows}


@pytest.mark.asyncio
async def test_the_first_html_already_holds_the_users_own_keys(
    database: Forwarder, key_holders: dict[str, str], served_page: ServedPage
) -> None:
    _, sent = await served_page(key_holders["user_token"], api_keys_page, _key_ids)
    assert sent == {key_holders["user_key"]}


@pytest.mark.asyncio
async def test_the_first_html_already_holds_every_key_for_an_admin(
    database: Forwarder, key_holders: dict[str, str], served_page: ServedPage
) -> None:
    _, sent = await served_page(key_holders["admin_token"], api_keys_page, _key_ids)
    assert {key_holders["admin_key"], key_holders["user_key"]} <= sent
