"""Pytest fixtures for webapp tests."""

import os
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app, core, storage
from nicegui.client import Client
from nicegui.page import page
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from openscientist.auth.middleware import require_auth
from openscientist.database.models import Session, User


@pytest.fixture(autouse=True)
def _nicegui_storage_dir(tmp_path):
    """Provide a temporary directory for NiceGUI storage during tests.

    NiceGUI persists user storage to .nicegui/ on disk. Without this,
    teardown fails with FileNotFoundError when async_backup runs.
    """
    storage_dir = tmp_path / ".nicegui"
    storage_dir.mkdir()
    with patch.dict(os.environ, {"NICEGUI_STORAGE_PATH": str(storage_dir)}):
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


OpenBrowser = Callable[[str | None], Awaitable[Client]]
OpenPage = Callable[[str, Callable[[], Awaitable[None]]], Awaitable[Client]]


@pytest.fixture
def browser_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[OpenBrowser]:
    """A NiceGUI client of its own whose request carries only the login
    cookie, or none when the token is None. Each client gets the user
    storage a deployed browser would have, removed with the client when the
    test ends.

    A deployed page never runs in NiceGUI's script mode, which an earlier test
    may have left on and which swaps the user storage for a blank one."""
    monkeypatch.setattr(core, "script_mode", False)
    opened: list[tuple[Client, str]] = []

    async def open_browser(token: str | None) -> Client:
        browser_id = str(uuid4())
        headers: list[tuple[bytes, bytes]] = []
        if token is not None:
            headers.append((b"cookie", f"session_token={token}".encode()))
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "query_string": b"",
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
