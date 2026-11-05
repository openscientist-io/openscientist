"""The /admin page built in a real signed-in NiceGUI client, read at the
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

from openscientist.database.models import Administrator, Session, User
from openscientist.webapp_components.pages.admin import admin_page, render_containers_panel
from openscientist.webapp_components.utils import container_dashboard
from openscientist.webapp_components.utils.container_dashboard import (
    ContainerInfo,
    DashboardData,
    JobContainerGroup,
)
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, WireMessage, drain_outbox


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
    yield {"token": str(login.id), "pending_id": str(pending.id)}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id.in_([admin.id, pending.id])))
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
        await render_containers_panel()

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
