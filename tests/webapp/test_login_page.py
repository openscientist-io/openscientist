"""The login page honors a still-valid session cookie.

After a web restart wipes the per-browser storage, the only credential the
visitor still holds is the session cookie, the same one every protected page
re-validates at build time.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app, ui
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Session, User
from openscientist.webapp_components.pages.login import login_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenBrowser


@pytest_asyncio.fixture
async def signed_in_visitor(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, str]]:
    """An approved user with a live session, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"login-{uuid4().hex}@page.test", name="Visitor", is_approved=True)
        db.add(user)
        await db.flush()
        login = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield user, str(login.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


async def _build_login_page() -> None:
    """Build the login page, awaiting its builder when it is async."""
    result = login_page()
    if inspect.isawaitable(result):
        await result


@pytest.mark.asyncio
async def test_the_login_page_repopulates_storage_from_a_valid_cookie(
    database: Forwarder,
    signed_in_visitor: tuple[User, str],
    browser_client: OpenBrowser,
) -> None:
    """A still-valid cookie signs the visitor back in after a web restart
    wiped the per-browser storage, instead of showing the form and minting a
    second session."""
    user, token = signed_in_visitor
    client = await browser_client(token)
    with client:
        # The container's recreate wiped the per-browser storage.
        app.storage.user.clear()
        app.storage.user["return_to"] = "/jobs"
        await _build_login_page()

    # The cookie alone repopulated the storage and steered the visitor on.
    assert app.storage.user["authenticated"] is True
    assert app.storage.user["user_id"] == str(user.id)
    assert app.storage.user["can_start_jobs"] is True
    assert "return_to" not in app.storage.user
    opened = [data for _target, kind, data in client.outbox.messages if kind == "open"]
    assert opened == [{"path": "/jobs", "new_tab": False}]
    assert not [e for e in client.layout.descendants() if isinstance(e, ui.card)]


@pytest.mark.asyncio
async def test_the_login_page_shows_the_form_without_a_cookie(
    browser_client: OpenBrowser,
) -> None:
    """A visitor with no session cookie still gets the sign-in form."""
    client = await browser_client(None)
    with client:
        await _build_login_page()
    assert app.storage.user.get("authenticated") is None
    assert [e for e in client.layout.descendants() if isinstance(e, ui.card)]
