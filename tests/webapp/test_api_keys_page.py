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
from sqlalchemy import delete, select
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
        admin_login, admin_login_token = Session.issue(user_id=admin.id, expires_at=expires)
        user_login, user_login_token = Session.issue(user_id=user.id, expires_at=expires)
        db.add_all([admin_key, user_key, admin_login, user_login])
        await db.commit()
    yield {
        "admin_token": admin_login_token,
        "user_token": user_login_token,
        "admin_key": str(admin_key.id),
        "user_key": str(user_key.id),
        "admin_id": str(admin.id),
        "user_id": str(user.id),
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


@pytest.mark.asyncio
async def test_demoted_admin_cannot_use_open_api_keys_page(
    database: Forwarder,
    key_holders: dict[str, str],
    served_page: ServedPage,
    test_engine: AsyncEngine,
) -> None:
    from uuid import UUID

    from openscientist.webapp_components.pages.api_keys import _query_api_keys, _revoke_key

    client, _ = await served_page(key_holders["admin_token"], api_keys_page, _key_ids)
    async with AsyncSession(test_engine) as db:
        login = await db.scalar(
            select(Session).where(Session.user_id == UUID(key_holders["admin_id"]))
        )
        assert login is not None
        admin_id = str(login.user_id)
        await db.execute(delete(Administrator).where(Administrator.user_id == login.user_id))
        await db.commit()
    with client:
        assert await _query_api_keys("", True, admin_id) == []
        assert not await _revoke_key(key_holders["user_key"], admin_id, True)
    async with AsyncSession(test_engine) as db:
        key = await db.get(APIKey, UUID(key_holders["user_key"]))
        assert key is not None and key.is_active


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff", ["revoked", "expired"])
async def test_reviewer_cannot_create_key_from_open_page(
    database: Forwarder,
    key_holders: dict[str, str],
    served_page: ServedPage,
    test_engine: AsyncEngine,
    cutoff: str,
) -> None:
    from uuid import UUID

    from openscientist.database.models import ReviewToken
    from openscientist.webapp_components.pages.admin import revoke_review_token
    from openscientist.webapp_components.pages.api_keys import _make_create_key_handler

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        login = await db.scalar(
            select(Session).where(Session.user_id == UUID(key_holders["user_id"]))
        )
        admin_login = await db.scalar(
            select(Session).where(Session.user_id == UUID(key_holders["admin_id"]))
        )
        assert login is not None and admin_login is not None
        token = ReviewToken(
            token_hash=uuid4().hex,
            label="Reviewer",
            created_by_id=admin_login.user_id,
            redeemed_by_id=login.user_id,
            redeemed_at=datetime.now(UTC),
        )
        db.add(token)
        await db.commit()
        user_id = login.user_id
    client, _ = await served_page(key_holders["user_token"], api_keys_page, _key_ids)
    if cutoff == "revoked":
        await revoke_review_token(token.id)
    else:
        async with AsyncSession(test_engine) as db:
            stored_token = await db.get(ReviewToken, token.id)
            assert stored_token is not None
            stored_token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()

    async def load_keys() -> None:
        pass

    with client:
        key_name = ui.input(value="forbidden")
        await _make_create_key_handler(
            key_name_input=key_name, user_id=str(user_id), load_keys=load_keys
        )()
    async with AsyncSession(test_engine) as db:
        keys = await db.scalars(
            select(APIKey).where(APIKey.user_id == user_id, APIKey.name == "forbidden")
        )
        assert list(keys) == []
