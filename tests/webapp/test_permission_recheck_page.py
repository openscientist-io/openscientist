"""Action-time permission rechecks inside a real signed-in NiceGUI page.

The page's request carries only the login cookie. The page is built behind
``require_auth`` in a NiceGUI client of its own, and the real handlers then
fire there, so the recheck reads the token the way a deployed page does.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app, core, events, storage, ui
from nicegui.client import Client
from nicegui.page import page
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.requests import Request

from openscientist.auth.middleware import require_auth
from openscientist.database.models import Administrator, Job, Session, User
from openscientist.webapp_components.components.dialogs import render_share_dialog
from openscientist.webapp_components.pages import admin as admin_page
from tests.db_outage import Forwarder


@pytest_asyncio.fixture
async def signed_in_admin(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, Job, str, User]]:
    """An admin who owns a private job, their live session's token, and an
    unapproved user, all committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@page.test", name="Page Owner", is_approved=True)
        pending = User(email=f"pending-{uuid4().hex}@page.test", name="Pending", is_approved=False)
        db.add_all([owner, pending])
        await db.flush()
        db.add(Administrator(user_id=owner.id))
        job = Job(owner_id=owner.id, research_question="Page recheck", status="completed")
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add_all([job, login])
        await db.commit()
    yield owner, job, str(login.id), pending
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, pending.id])))
        await db.commit()


OpenPage = Callable[[str, Callable[[], Awaitable[None]]], Awaitable[Client]]


@pytest.fixture
def signed_in_page(monkeypatch: pytest.MonkeyPatch) -> Iterator[OpenPage]:
    """Open pages whose request carries only the login cookie, each a NiceGUI
    client of its own with ``build`` run behind ``require_auth``, removed with
    their user storage when the test ends.

    A deployed page never runs in NiceGUI's script mode, which an earlier test
    may have left on and which swaps the user storage for a blank one."""
    monkeypatch.setattr(core, "script_mode", False)
    opened: list[tuple[Client, str]] = []

    async def open_page(token: str, build: Callable[[], Awaitable[None]]) -> Client:
        browser_id = str(uuid4())
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "query_string": b"",
                "headers": [(b"cookie", f"session_token={token}".encode())],
                "session": {"id": browser_id},
                "client": ("127.0.0.1", 50000),
                "server": ("127.0.0.1", 8080),
            }
        )
        await app.storage._create_user_storage(browser_id)
        storage.request_contextvar.set(request)
        client = Client(page("/"), request=request)
        opened.append((client, browser_id))
        with client:
            await require_auth(build)()
        return client

    yield open_page
    for client, browser_id in opened:
        client.delete()
        app.storage._users.pop(browser_id, None)


async def _fire(handler: Callable[..., Any], arguments: events.UiEventArguments) -> None:
    """Run a page handler the way NiceGUI does, in its sender's slot, and await it to the end."""
    with arguments.sender.parent_slot or nullcontext():
        result = handler(arguments)
        if inspect.isawaitable(result):
            await result


@pytest.mark.asyncio
async def test_owner_changes_visibility_from_a_signed_in_page(
    database: Forwarder,
    test_engine: AsyncEngine,
    signed_in_admin: tuple[User, Job, str, User],
    signed_in_page: OpenPage,
) -> None:
    """Flipping the share dialog's public switch on the owner's own page makes the job public."""
    _, job, token, _ = signed_in_admin
    built: list[ui.dialog] = []

    async def build() -> None:
        built.append(render_share_dialog(str(job.id)))

    client = await signed_in_page(token, build)
    switch = next(e for e in built[0].descendants() if isinstance(e, ui.switch))
    with client:
        for handler in switch._change_handlers:
            await _fire(
                handler,
                events.ValueChangeEventArguments(
                    sender=switch, client=client, value=True, previous_value=False
                ),
            )

    async with AsyncSession(test_engine) as db:
        row = await db.get(Job, job.id)
        assert row is not None
        assert row.is_public is True


@pytest.mark.asyncio
async def test_admin_approves_a_user_from_a_signed_in_page(
    database: Forwarder,
    test_engine: AsyncEngine,
    signed_in_admin: tuple[User, Job, str, User],
    signed_in_page: OpenPage,
) -> None:
    """An admin action fired from the admin's own page passes the admin recheck."""
    _, _, token, pending = signed_in_admin
    built: list[ui.button] = []

    async def reload_users() -> None:
        return None

    async def approve(_: events.ClickEventArguments) -> None:
        event = type("Event", (), {"args": {"id": str(pending.id)}})()
        await admin_page._handle_approval_update(event, is_approved=True, reload_users=reload_users)

    async def build() -> None:
        built.append(ui.button("Approve", on_click=approve))

    client = await signed_in_page(token, build)
    button = built[0]
    with client:
        await _fire(approve, events.ClickEventArguments(sender=button, client=client))

    async with AsyncSession(test_engine) as db:
        row = await db.get(User, pending.id)
        assert row is not None
        assert row.is_approved is True
