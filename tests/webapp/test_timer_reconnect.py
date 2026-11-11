"""Page timers across a websocket blip: a reconnect inside the window keeps
them firing, and a client that never returns stops them when it is deleted."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import core, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, Session, User
from openscientist.webapp_components.pages.jobs_list import jobs_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage


@pytest_asyncio.fixture
async def jobs_owner(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An approved user who owns one job and holds a live session, all committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"reconnect-{uuid4().hex}@timer.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        first = Job(owner_id=owner.id, research_question="First question", status="completed")
        db.add(first)
        await db.flush()
        login, login_token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(login)
        await db.commit()
    yield {
        "user_id": str(owner.id),
        "token": login_token,
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.owner_id == owner.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page hands the job manager only to its delete dialog."""
    monkeypatch.setattr(web_app._state, "job_manager", object())


@pytest.fixture
def _reconnect_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 3 s reconnect window a ui.run deployment waits before deleting a client."""
    monkeypatch.setattr(core.app.config, "reconnect_timeout", 3.0, raising=False)


async def _insert_job(test_engine: AsyncEngine, user_id: str, question: str) -> str:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        job = Job(owner_id=UUID(user_id), research_question=question, status="completed")
        db.add(job)
        await db.commit()
        return str(job.id)


def _my_table(client: Client) -> ui.table:
    tables = [e for e in client.elements.values() if isinstance(e, ui.table)]
    return next(t for t in tables if not any(c["name"] == "owner" for c in t.columns))


def _row_ids(table: ui.table) -> set[str]:
    return {row["job_id"] for row in table.rows}


@pytest.mark.asyncio
async def test_a_reconnected_client_keeps_its_page_timers(
    database: Forwarder,
    test_engine: AsyncEngine,
    jobs_owner: dict[str, str],
    served_page: ServedPage,
    _reconnect_window: None,
) -> None:
    """A socket blip inside the reconnect window keeps the page alive, and its
    refresh timers keep firing on the reconnected client."""
    client, _ = await served_page(jobs_owner["token"], jobs_page, lambda c: _row_ids(_my_table(c)))
    table = _my_table(client)
    await client.handle_handshake("sim-socket", "tab-1", None, "doc-1", None)
    await asyncio.sleep(0.5)
    new_job = await _insert_job(test_engine, jobs_owner["user_id"], "After the blip")
    client.handle_disconnect("sim-socket")
    await asyncio.sleep(1.0)
    assert not client.is_deleted
    await client.handle_handshake("sim-socket-2", "tab-1", None, "doc-1", None)
    await asyncio.sleep(11.0)
    assert new_job in _row_ids(table)


@pytest.mark.asyncio
async def test_a_client_that_never_returns_stops_its_page_timers(
    database: Forwarder,
    test_engine: AsyncEngine,
    jobs_owner: dict[str, str],
    served_page: ServedPage,
    _reconnect_window: None,
) -> None:
    """When the socket drops and the browser never returns, the client is
    deleted after the window and its timers stop with it."""
    client, _ = await served_page(jobs_owner["token"], jobs_page, lambda c: _row_ids(_my_table(c)))
    table = _my_table(client)
    await client.handle_handshake("sim-socket", "tab-1", None, "doc-1", None)
    await asyncio.sleep(0.5)
    client.handle_disconnect("sim-socket")
    await asyncio.sleep(4.0)
    assert client.is_deleted
    new_job = await _insert_job(test_engine, jobs_owner["user_id"], "After the close")
    await asyncio.sleep(11.0)
    assert new_job not in _row_ids(table)
