"""The /jobs page built in a real signed-in NiceGUI client, read at the
moment NiceGUI sends it (see ``served_page``)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ElementFilter, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, JobShare, Session, User
from openscientist.settings import clear_settings_cache
from openscientist.webapp_components.pages.jobs_list import jobs_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, WireMessage, drain_outbox


@pytest_asyncio.fixture
async def jobs_owner(test_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """An approved user who owns two jobs, has one shared with them, and holds
    a live session, all committed. A second approved user has no jobs."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@jobs.test", name="Owner", is_approved=True)
        sharer = User(email=f"sharer-{uuid4().hex}@jobs.test", name="Sharer", is_approved=True)
        newcomer = User(email=f"new-{uuid4().hex}@jobs.test", name="Newcomer", is_approved=True)
        db.add_all([owner, sharer, newcomer])
        await db.flush()
        first = Job(owner_id=owner.id, research_question="First question", status="completed")
        second = Job(owner_id=owner.id, research_question="Second question", status="running")
        theirs = Job(owner_id=sharer.id, research_question="Shared question", status="completed")
        db.add_all([first, second, theirs])
        await db.flush()
        db.add(JobShare(job_id=theirs.id, shared_with_user_id=owner.id, permission_level="view"))
        owner_login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        newcomer_login = Session(
            user_id=newcomer.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add_all([owner_login, newcomer_login])
        await db.commit()
    yield {
        "owned": {str(first.id), str(second.id)},
        "shared": {str(theirs.id)},
        "owner_token": str(owner_login.id),
        "newcomer_token": str(newcomer_login.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id.in_([first.id, second.id, theirs.id])))
        await db.execute(delete(User).where(User.id.in_([owner.id, sharer.id, newcomer.id])))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page hands the job manager only to its delete dialog."""
    monkeypatch.setattr(web_app._state, "job_manager", object())


@pytest.fixture
def configured_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An Anthropic key, so the page offers starting a job."""
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    clear_settings_cache()
    yield
    clear_settings_cache()


async def _open_jobs_page(served_page: ServedPage, token: str) -> tuple[Client, set[str]]:
    """Build /jobs, returning the client and the owned job ids the first HTML carried."""
    client, sent_ids = await served_page(
        token, jobs_page, lambda client: _row_ids(_table(client, shared=False))
    )
    return client, sent_ids


def _table(client: Client, *, shared: bool) -> ui.table:
    tables = [e for e in client.elements.values() if isinstance(e, ui.table)]
    return next(t for t in tables if any(c["name"] == "owner" for c in t.columns) is shared)


def _marked(client: Client, marker: str) -> ui.element:
    with client:
        return next(iter(ElementFilter(marker=marker)))


def _row_ids(table: ui.table) -> set[str]:
    return {row["job_id"] for row in table.rows}


@pytest.mark.asyncio
async def test_the_first_html_already_holds_the_users_jobs(
    database: Forwarder,
    jobs_owner: dict[str, Any],
    served_page: ServedPage,
) -> None:
    """Both tables are filled before NiceGUI sends the page."""
    client, sent_ids = await _open_jobs_page(served_page, jobs_owner["owner_token"])
    assert sent_ids == jobs_owner["owned"]
    assert _row_ids(_table(client, shared=True)) == jobs_owner["shared"]
    assert not _marked(client, "owned-jobs-empty").visible


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured_provider")
async def test_a_user_without_jobs_gets_the_empty_state_instead_of_an_empty_table(
    database: Forwarder,
    jobs_owner: dict[str, Any],
    served_page: ServedPage,
) -> None:
    """An approved user with no jobs sees the empty state and its way to start one."""
    client, _ = await _open_jobs_page(served_page, jobs_owner["newcomer_token"])
    owned_empty = _marked(client, "owned-jobs-empty")
    assert owned_empty.visible
    assert not _table(client, shared=False).visible
    assert any(isinstance(e, ui.button) for e in owned_empty.descendants())
    assert _marked(client, "shared-jobs-empty").visible
    assert not _table(client, shared=True).visible


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_rows_and_says_so_until_the_next_success(
    database: Forwarder,
    jobs_owner: dict[str, Any],
    served_page: ServedPage,
) -> None:
    client, _ = await _open_jobs_page(served_page, jobs_owner["owner_token"])
    table = _table(client, shared=False)
    error = _marked(client, "owned-jobs-load-error")
    refreshes: list[Callable[[], Awaitable[None]]] = []
    for element in client.elements.values():
        if isinstance(element, ui.timer) and element.interval == 5.0:
            assert element.callback is not None
            refreshes.append(element.callback)
    assert not error.visible

    database.down()
    with client:
        for refresh in refreshes:
            await refresh()
    assert _row_ids(table) == jobs_owner["owned"]
    assert table.visible
    assert error.visible
    assert not _marked(client, "owned-jobs-empty").visible

    database.up()
    with client:
        for refresh in refreshes:
            await refresh()
    assert not error.visible


@pytest.mark.asyncio
async def test_an_unchanged_tick_sends_no_update_messages(
    database: Forwarder,
    jobs_owner: dict[str, Any],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
) -> None:
    """A 5 s tick whose rows did not change puts no update message on the wire."""
    client, _ = await _open_jobs_page(served_page, jobs_owner["owner_token"])
    refreshes: list[Callable[[], Awaitable[None]]] = []
    for element in client.elements.values():
        if isinstance(element, ui.timer) and element.interval == 5.0:
            assert element.callback is not None
            refreshes.append(element.callback)
    assert len(refreshes) == 2

    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    first = [m for m in wire_recorder if m.event == "update" and m.room == client.id]
    assert first, "the build's elements never reached the wire"

    wire_recorder.clear()
    with client:
        for refresh in refreshes:
            await refresh()
    await drain_outbox(client)
    sent = [m for m in wire_recorder if m.event == "update" and m.room == client.id]
    assert not sent, f"an unchanged tick sent {sum(m.wire_bytes for m in sent)} wire bytes"
