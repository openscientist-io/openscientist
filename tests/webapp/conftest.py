"""Pytest fixtures for webapp tests."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app, background_tasks, core, storage, ui
from nicegui.app.app import State
from nicegui.client import Client
from nicegui.page import page
from socketio import AsyncServer  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from openscientist.auth.middleware import require_auth
from openscientist.database.models import Session, User


@pytest.fixture(autouse=True)
def _nicegui_storage_dir(tmp_path: Path) -> Iterator[None]:
    """Keep NiceGUI persistence inside the test's directory."""
    storage_dir = tmp_path / ".nicegui"
    storage_dir.mkdir()
    with (
        patch.dict(os.environ, {"NICEGUI_STORAGE_PATH": str(storage_dir)}),
        patch.object(storage.Storage, "path", storage_dir),
        patch.object(app, "storage", storage.Storage()),
    ):
        yield


@pytest_asyncio.fixture
async def webapp_user(db_session: AsyncSession) -> User:
    """Create a test user for webapp tests."""
    user = User(
        email="webapp-test@example.com",
        name="Webapp Test User",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def webapp_session(db_session: AsyncSession, webapp_user: User) -> Session:
    """Create a valid session for the webapp test user."""
    session = Session(
        user_id=webapp_user.id,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


class OpenBrowser(Protocol):
    """Open a NiceGUI client whose request asks for ``url`` with the login
    cookie ``token``, sharing the per-browser storage of ``browser_id`` when
    one is given, as the tabs of one real browser do."""

    def __call__(
        self, token: str | None, url: str = "/", browser_id: str | None = None
    ) -> Awaitable[Client]: ...


OpenPage = Callable[[str, Callable[[], Awaitable[None]]], Awaitable[Client]]


@pytest.fixture
def browser_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[OpenBrowser]:
    """A NiceGUI client of its own whose request asks for ``url`` and carries
    only the login cookie, or none when the token is None. Each client gets
    the user storage a deployed browser would have, removed with the client
    when the test ends.

    A deployed page never runs in NiceGUI's script mode, which an earlier test
    may have left on and which swaps the user storage for a blank one."""
    monkeypatch.setattr(core, "script_mode", False)
    opened: list[tuple[Client, str]] = []

    async def open_browser(
        token: str | None, url: str = "/", browser_id: str | None = None
    ) -> Client:
        if browser_id is None:
            browser_id = str(uuid4())
        headers: list[tuple[bytes, bytes]] = []
        if token is not None:
            headers.append((b"cookie", f"session_token={token}".encode()))
        path, _, query = url.partition("?")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "query_string": query.encode(),
                "headers": headers,
                "session": {"id": browser_id},
                "client": ("127.0.0.1", 50000),
                "server": ("127.0.0.1", 8080),
            }
        )
        await app.storage._create_user_storage(browser_id)
        storage.request_contextvar.set(request)
        client = Client(page("/"), request=request)
        opened.append((client, browser_id))
        return client

    yield open_browser
    for client, browser_id in opened:
        if not client.is_deleted:
            client.delete()
        app.storage._users.pop(browser_id, None)


@pytest.fixture
def signed_in_page(browser_client: OpenBrowser) -> Iterator[OpenPage]:
    """Open pages behind ``require_auth`` on a browser whose request carries
    only the login cookie."""

    async def open_page(token: str, build: Callable[[], Awaitable[None]]) -> Client:
        client = await browser_client(token)
        with client:
            await require_auth(build)()
        return client

    yield open_page


ServedPage = Callable[
    [str, Callable[[], Awaitable[Any]], Callable[[Client], Any]], Awaitable[tuple[Client, Any]]
]


@pytest_asyncio.fixture
async def served_page(
    browser_client: OpenBrowser, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[ServedPage]:
    """Build a page the way NiceGUI serves it, returning the client and what
    ``read`` saw in it at the moment NiceGUI sends the first HTML.

    NiceGUI sends an async page once it returns or once anything on it awaits
    the browser's connection, as every ``ui.timer`` does, whichever comes
    first. Its background tasks run on the test's loop as in a started app,
    since timers neither start on a missing loop nor run in a stopped app.
    The client never connects, so no timer fires, and the build then runs to
    its end."""
    monkeypatch.setattr(core, "loop", asyncio.get_running_loop())
    monkeypatch.setattr(core.app, "_state", State.STARTED)

    async def open_served(
        token: str, build_page: Callable[[], Awaitable[Any]], read: Callable[[Client], Any]
    ) -> tuple[Client, Any]:
        client = await browser_client(token)

        async def build() -> None:
            with client:
                await build_page()

        built = asyncio.create_task(build())
        waiting = asyncio.create_task(client._waiting_for_connection.wait())
        done, _ = await asyncio.wait(
            {built, waiting}, timeout=10, return_when=asyncio.FIRST_COMPLETED
        )
        assert done, "the page was neither built nor sent within 10 s"
        sent = read(client)
        waiting.cancel()
        await built
        return client, sent

    yield open_served


@dataclass(frozen=True)
class WireMessage:
    """One message the outbox would deliver to a connected browser."""

    event: str
    room: str | None
    wire_bytes: int


@pytest.fixture
def wire_recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[WireMessage]]:
    """Record every message NiceGUI would deliver to a connected browser.

    The client never connects during the test, so the outbox loop holds its
    queue until the test gives the client a ``tab_id``. From then on it
    delivers to the recorder in place of the socket layer, the way the probe
    of the original finding measured the wire."""
    records: list[WireMessage] = []

    class _Eio:
        ping_interval = 25
        ping_timeout = 20

    class _Stub:
        eio = _Eio()

        async def emit(
            self, event: str, data: Mapping[str, object], room: str | None = None
        ) -> None:
            records.append(WireMessage(event, room, len(json.dumps(data).encode())))

    monkeypatch.setattr(core, "sio", cast(AsyncServer, _Stub()), raising=False)
    yield records


async def drain_outbox(client: Client) -> None:
    """Wait for the outbox to hand everything queued so far to the wire recorder."""
    for _ in range(500):
        if not client.outbox.updates and not client.outbox.messages:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)


async def fire_click_twice(
    button: ui.button, client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fire the button's click twice the way NiceGUI's socket handler does,
    so the two handler tasks race on the event loop."""
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    handler = listener.handler
    scheduled: list[Awaitable[None]] = []

    def _capture(awaitable: Awaitable[None], *, name: str = "") -> None:
        scheduled.append(awaitable)

    monkeypatch.setattr(background_tasks, "create_or_defer", _capture)
    storage.request_contextvar.set(listener.request)
    with client:
        with button.parent_slot or nullcontext():
            handler(None)
            handler(None)
    assert len(scheduled) == 2
    await asyncio.gather(*scheduled)
