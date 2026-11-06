"""Periodic page ticks re-check the session the page was built with.

A tick that keeps running for a signed-out user would keep refreshing that
user's data on their open tab, so the periodic ticks of the /jobs list and
the job page poll, and the admin containers tick, re-read the login cookie
against the sessions table before touching data. A tick that finds the
session gone stops its timer and sends the client to the login page,
carrying the page it leaves as ``next``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from nicegui.element import Element
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import (
    Administrator,
    Job,
    JobShare,
    Session,
    User,
)
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.admin import render_containers_panel
from openscientist.webapp_components.pages.job_detail import job_detail_page
from openscientist.webapp_components.pages.jobs_list import jobs_page
from openscientist.webapp_components.utils import container_dashboard
from openscientist.webapp_components.utils.container_dashboard import DashboardData
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, WireMessage, drain_outbox

# The test client's request asks for "/", the way browser_client builds it.
LOGIN_TARGET = f"/login?next={quote('/')}"


@pytest_asyncio.fixture
async def tick_user(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An approved user, a running job they own and their live session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"tick-{uuid4().hex}@tick.test", name="Tick", is_approved=True)
        db.add(user)
        await db.flush()
        job = Job(owner_id=user.id, research_question="Tick question", status="running")
        db.add(job)
        await db.flush()
        login = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {"user_id": str(user.id), "job_id": str(job.id), "token": str(login.id)}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Session).where(Session.user_id == user.id))
        # Every job the user owns, including those a test inserts.
        await db.execute(delete(Job).where(Job.owner_id == user.id))
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


@pytest_asyncio.fixture
async def tick_admin(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An administrator and their live session, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(email=f"admin-{uuid4().hex}@tick.test", name="Admin", is_approved=True)
        db.add(admin)
        await db.flush()
        db.add(Administrator(user_id=admin.id))
        login = Session(user_id=admin.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {"token": str(login.id), "user_id": str(admin.id)}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Session).where(Session.user_id == admin.id))
        await db.execute(delete(Administrator).where(Administrator.user_id == admin.id))
        await db.execute(delete(User).where(User.id == admin.id))
        await db.commit()


@pytest_asyncio.fixture
async def feedback_viewer(
    test_engine: AsyncEngine,
) -> AsyncIterator[dict[str, str]]:
    """A job awaiting feedback, shared view-only to a second user who holds a
    live session, so their page shows the countdown instead of the input."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@tick.test", name="Owner", is_approved=True)
        viewer = User(email=f"viewer-{uuid4().hex}@tick.test", name="Viewer", is_approved=True)
        db.add_all([owner, viewer])
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="Feedback question",
            status="awaiting_feedback",
            started_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        db.add(job)
        await db.flush()
        db.add(JobShare(job_id=job.id, shared_with_user_id=viewer.id, permission_level="view"))
        login = Session(user_id=viewer.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {"job_id": str(job.id), "viewer_id": str(viewer.id), "token": str(login.id)}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Session).where(Session.user_id == viewer.id))
        await db.execute(delete(JobShare).where(JobShare.job_id == job.id))
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, viewer.id])))
        await db.commit()


@pytest.fixture(autouse=True)
def job_manager_status(monkeypatch: pytest.MonkeyPatch, tmp_path) -> dict[str, JobStatus]:
    """The page's job manager, whose job status a test sets to match its job."""
    state: dict[str, JobStatus] = {"status": JobStatus.RUNNING}
    started = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=state["status"], started_at=started)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )
    return state


def _navigation_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    targets: list[str] = []
    original = ui.navigate.to

    def spy(target: Callable[..., object] | str | Element, new_tab: bool = False) -> None:
        if isinstance(target, str):
            targets.append(target)
        original(target, new_tab)

    monkeypatch.setattr(ui.navigate, "to", spy)
    return targets


def _timer_with_interval(client: Client, interval: float) -> list[ui.timer]:
    return [
        element
        for element in client.elements.values()
        if isinstance(element, ui.timer) and element.interval == interval
    ]


def _timer_by_callback_name(client: Client, interval: float, name: str) -> ui.timer:
    for timer in _timer_with_interval(client, interval):
        assert timer.callback is not None
        if timer.callback.__name__ == name:
            return timer
    raise AssertionError(f"no {interval} s timer with a callback named {name!r}")


async def _deliver(client: Client) -> None:
    """Give the client its socket, then deliver what the ticks queued.

    A timer starts its own schedule only once its client has a socket, so
    the timers stay idle while the test fires their ticks by hand."""
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)


def _owned_jobs_table(client: Client) -> ui.table:
    tables = [e for e in client.elements.values() if isinstance(e, ui.table)]
    return next(t for t in tables if not any(c["name"] == "owner" for c in t.columns))


def _row_ids(table: ui.table) -> set[str]:
    return {row["job_id"] for row in table.rows}


async def _drop_session_row(test_engine: AsyncEngine, token: str) -> None:
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Session).where(Session.id == UUID(token)))
        await db.commit()


async def _insert_job(test_engine: AsyncEngine, user_id: str, question: str) -> str:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        job = Job(owner_id=UUID(user_id), research_question=question, status="running")
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return str(job.id)


@pytest.mark.asyncio
async def test_the_jobs_tick_sends_the_page_to_login_once_the_session_is_gone(
    database: Forwarder,
    tick_user: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5 s tick whose session row is gone stops the timer, sends the client
    to the login page and stops refreshing the user's rows."""
    targets = _navigation_spy(monkeypatch)
    client, _ = await served_page(tick_user["token"], jobs_page, lambda c: None)
    assert len(_timer_with_interval(client, 5.0)) == 2
    owned_tick = _timer_by_callback_name(client, 5.0, "refresh_my_jobs_table")
    owned_callback = owned_tick.callback
    assert owned_callback is not None

    await _drop_session_row(test_engine, tick_user["token"])

    with client:
        await owned_callback()
    assert owned_tick.active is False
    await _deliver(client)
    assert targets == [LOGIN_TARGET]
    assert [m for m in wire_recorder if m.event == "open" and m.room == client.id]

    # A job started while the user is signed out must not reach the table.
    await _insert_job(test_engine, tick_user["user_id"], "Started while signed out")
    with client:
        await owned_callback()
    assert _row_ids(_owned_jobs_table(client)) == {tick_user["job_id"]}


@pytest.mark.asyncio
async def test_the_jobs_tick_keeps_refreshing_while_the_session_is_valid(
    database: Forwarder,
    tick_user: dict[str, str],
    served_page: ServedPage,
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 5 s tick with a live session refreshes the rows and never navigates."""
    targets = _navigation_spy(monkeypatch)
    client, _ = await served_page(tick_user["token"], jobs_page, lambda c: None)
    owned_tick = _timer_by_callback_name(client, 5.0, "refresh_my_jobs_table")
    owned_callback = owned_tick.callback
    assert owned_callback is not None

    new_job = await _insert_job(test_engine, tick_user["user_id"], "Started after the build")
    with client:
        await owned_callback()
    assert _row_ids(_owned_jobs_table(client)) == {tick_user["job_id"], new_job}
    assert owned_tick.active is True
    assert targets == []


@pytest.mark.asyncio
async def test_the_job_page_poll_sends_the_page_to_login_once_the_session_is_gone(
    database: Forwarder,
    tick_user: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2 s poll with a valid session keeps polling; once the session row is
    gone the next poll stops the timer and sends the client to the login page."""
    targets = _navigation_spy(monkeypatch)
    client, _ = await served_page(
        tick_user["token"], lambda: job_detail_page(tick_user["job_id"]), lambda c: None
    )
    polls = _timer_with_interval(client, 2.0)
    assert len(polls) == 1
    poll = polls[0]
    poll_callback = poll.callback
    assert poll_callback is not None

    with client:
        await poll_callback()
    assert poll.active is True
    assert targets == []

    await _drop_session_row(test_engine, tick_user["token"])
    with client:
        await poll_callback()
    assert poll.active is False
    await _deliver(client)
    assert targets == [LOGIN_TARGET]
    assert [m for m in wire_recorder if m.event == "open" and m.room == client.id]


@pytest.mark.asyncio
async def test_the_admin_tick_sends_the_page_to_login_once_the_session_is_gone(
    database: Forwarder,
    tick_admin: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3 s containers tick with a valid session keeps ticking; once the
    session row is gone the next tick stops the timer and sends the client to
    the login page."""
    payloads: list[DashboardData] = [DashboardData(docker_available=True, job_groups=[])]

    async def collect(include_stats: bool = True) -> DashboardData:
        return payloads[0]

    monkeypatch.setattr(container_dashboard, "collect_dashboard_data", collect)
    targets = _navigation_spy(monkeypatch)
    # Only the containers panel is served. The admin page's other deferred
    # panels start once the client has a socket and would race the ticks.
    client, _ = await served_page(
        tick_admin["token"],
        lambda: render_containers_panel(tick_admin["user_id"]),
        lambda c: None,
    )
    ticks = _timer_with_interval(client, 3.0)
    assert len(ticks) == 1
    tick = ticks[0]
    tick_callback = tick.callback
    assert tick_callback is not None

    with client:
        await tick_callback()
    assert tick.active is True
    assert targets == []

    await _drop_session_row(test_engine, tick_admin["token"])
    with client:
        await tick_callback()
    assert tick.active is False
    await _deliver(client)
    assert targets == [LOGIN_TARGET]
    assert [m for m in wire_recorder if m.event == "open" and m.room == client.id]


@pytest.mark.asyncio
async def test_the_job_page_poll_sends_a_view_only_viewer_to_login_once_their_session_is_gone(
    database: Forwarder,
    feedback_viewer: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    job_manager_status: dict[str, JobStatus],
) -> None:
    """A view-only viewer's poll of a job awaiting feedback also ends the
    page on their login once their session row is gone."""
    job_manager_status["status"] = JobStatus.AWAITING_FEEDBACK
    targets = _navigation_spy(monkeypatch)
    client, _ = await served_page(
        feedback_viewer["token"],
        lambda: job_detail_page(feedback_viewer["job_id"]),
        lambda c: None,
    )
    polls = _timer_with_interval(client, 2.0)
    assert len(polls) == 1
    poll = polls[0]
    poll_callback = poll.callback
    assert poll_callback is not None

    with client:
        await poll_callback()
    assert poll.active is True
    assert targets == []

    await _drop_session_row(test_engine, feedback_viewer["token"])
    with client:
        await poll_callback()
    assert poll.active is False
    await _deliver(client)
    assert targets == [LOGIN_TARGET]
    assert [m for m in wire_recorder if m.event == "open" and m.room == client.id]
