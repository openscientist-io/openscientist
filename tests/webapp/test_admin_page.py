"""The /admin page built in a real signed-in NiceGUI client, read at the
moment NiceGUI sends it (see ``served_page``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import app, ui
from nicegui.client import Client
from nicegui.functions.refreshable import RefreshableContainer
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, ReviewToken, Session, User
from openscientist.webapp_components.pages.admin import admin_page, render_containers_panel
from openscientist.webapp_components.utils import container_dashboard
from openscientist.webapp_components.utils.container_dashboard import (
    ContainerInfo,
    DashboardData,
    JobContainerGroup,
)
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenPage, ServedPage, WireMessage, drain_outbox, fire_click_twice


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
    yield {"token": str(login.id), "pending_id": str(pending.id), "admin_id": str(admin.id)}
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


def _agent_container() -> ContainerInfo:
    return ContainerInfo(
        container_id="abc123",
        name="openscientist-agent-job-1",
        container_type="agent",
        job_id="job-1",
        status="exited",
        created_at=None,
    )


def _dashboard_payload(*, extra_group: bool = False) -> DashboardData:
    """A launcher payload with one job group, optionally a second."""
    groups = [
        JobContainerGroup(
            job_id="job-1",
            title="First question",
            status="completed",
            owner_email="owner@example.com",
            current_iteration=1,
            max_iterations=5,
            agent_container=_agent_container(),
        )
    ]
    if extra_group:
        groups.append(
            JobContainerGroup(
                job_id="job-2",
                title="Second question",
                status="running",
                owner_email="owner@example.com",
                current_iteration=3,
                max_iterations=5,
            )
        )
    return DashboardData(docker_available=True, job_groups=groups)


@pytest.mark.asyncio
async def test_an_unchanged_container_tick_sends_no_update_messages(
    database: Forwarder,
    admin_with_a_pending_user: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3 s tick whose container data did not change puts no update on the wire."""
    payloads: list[DashboardData] = [_dashboard_payload()]

    async def collect(include_stats: bool = True) -> DashboardData:
        return payloads[0]

    monkeypatch.setattr(container_dashboard, "collect_dashboard_data", collect)

    client, _ = await served_page(
        admin_with_a_pending_user["token"], admin_page, lambda client: None
    )
    with client:
        await render_containers_panel(admin_with_a_pending_user["admin_id"])

    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    first = [m for m in wire_recorder if m.event == "update" and m.room == client.id]
    assert first, "the panel's elements never reached the wire"

    timer = next(
        element
        for element in client.elements.values()
        if isinstance(element, ui.timer) and element.interval == 3.0
    )
    assert timer.callback is not None

    wire_recorder.clear()
    with client:
        await timer.callback()
    await drain_outbox(client)
    sent = [m for m in wire_recorder if m.event == "update" and m.room == client.id]
    assert not sent, f"an unchanged tick sent {sum(m.wire_bytes for m in sent)} wire bytes"

    payloads[0] = _dashboard_payload(extra_group=True)
    with client:
        await timer.callback()
    await drain_outbox(client)
    resent = [m for m in wire_recorder if m.event == "update" and m.room == client.id]
    assert resent, "a changed payload never reached the wire"


def _timers_with_interval(client: Client, interval: float) -> list[ui.timer]:
    return [
        element
        for element in client.elements.values()
        if isinstance(element, ui.timer) and element.interval == interval
    ]


def _deferred_containers_timer(client: Client) -> ui.timer:
    """The 0.1 s once timer the admin page schedules for the Containers panel."""
    for element in client.elements.values():
        callback = getattr(element, "callback", None)
        if (
            isinstance(element, ui.timer)
            and callback is not None
            and getattr(callback, "func", callback) is render_containers_panel
        ):
            return element
    raise AssertionError("the admin page scheduled no timer for the Containers panel")


def _dashboard_refreshable(client: Client) -> bool:
    return any(isinstance(element, RefreshableContainer) for element in client.elements.values())


@pytest.mark.asyncio
async def test_a_signout_before_the_deferred_containers_render_renders_then_logins(
    database: Forwarder,
    test_engine: AsyncEngine,
    admin_with_a_pending_user: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sign-out in another tab that clears the page's user storage and deletes
    the session row before the deferred Containers render runs must not make that
    render raise. The panel renders from the id the page was built with, and its
    first session tick sends the browser to login."""
    token = admin_with_a_pending_user["token"]

    async def collect(include_stats: bool = True) -> DashboardData:
        return _dashboard_payload()

    monkeypatch.setattr(container_dashboard, "collect_dashboard_data", collect)
    client, _ = await served_page(token, admin_page, lambda client: None)

    # Sign out in another tab: clear this browser's user storage and delete the
    # session row before the deferred render fires.
    app.storage.user.clear()
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Session).where(Session.id == UUID(token)))
        await db.commit()

    containers_timer = _deferred_containers_timer(client)
    assert containers_timer.callback is not None
    with client:
        await containers_timer.callback()

    # The render finished with the captured id, so the dashboard is present and
    # the session-checked timer it installs exists.
    assert _dashboard_refreshable(client), "the Containers panel never rendered"
    ticks = _timers_with_interval(client, 3.0)
    assert len(ticks) == 1, "the containers render did not reach its session timer"

    tick = ticks[0]
    assert tick.callback is not None
    client.tab_id = "signout-socket"
    wire_recorder.clear()
    with client:
        await tick.callback()
    assert tick.active is False, "the tick kept ticking after the session was gone"
    await drain_outbox(client)
    assert [m for m in wire_recorder if m.event == "open" and m.room == client.id], (
        "the first tick did not send the browser to login"
    )


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
