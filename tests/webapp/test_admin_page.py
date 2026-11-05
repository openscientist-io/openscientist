"""The /admin page built in a real signed-in NiceGUI client, read at the
moment NiceGUI sends it (see ``served_page``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, tzinfo
from typing import ClassVar, Self
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, Job, ReviewToken, Session, User
from openscientist.job_container.launcher_client import ContainerEntry
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
    yield {"token": str(login.id), "pending_id": str(pending.id)}
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


class _FakeNow(datetime):
    """A datetime whose ``now`` the test moves, standing in for the tick clock."""

    value: ClassVar[datetime]

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        return cls.fromtimestamp(cls.value.timestamp(), tz)


# Two CPUs, so cpu_percent is cpu_delta / system_delta * 200.
_CPU_SYSTEM_DELTA = 10_000_000_000
_CPU_NANOS_12_PERCENT = 617_000_000
_CPU_NANOS_55_PERCENT = 2_785_000_000
_MEMORY_BYTES_512_MB = 537_290_342
_MEMORY_LIMIT_BYTES = 8_589_934_592


class _FakeLauncher:
    """A launcher client reporting one container with fresh stats per call."""

    def __init__(self, entry: ContainerEntry) -> None:
        self.entry = entry
        self.cpu_nanos = _CPU_NANOS_12_PERCENT
        self.stats_calls = 0

    def health(self) -> dict[str, object]:
        return {"status": "ok"}

    def list_containers(self, labels: list[str]) -> list[ContainerEntry]:
        return [self.entry]

    def stats(self, ref: str) -> dict[str, object]:
        self.stats_calls += 1
        return {
            "cpu_stats": {
                "cpu_usage": {
                    "total_usage": self.cpu_nanos + self.stats_calls,
                    "percpu_usage": [0, 0],
                },
                "system_cpu_usage": _CPU_SYSTEM_DELTA,
            },
            "precpu_stats": {"cpu_usage": {"total_usage": 0}, "system_cpu_usage": 0},
            "memory_stats": {
                "usage": _MEMORY_BYTES_512_MB + self.stats_calls,
                "stats": {"cache": 0},
                "limit": _MEMORY_LIMIT_BYTES,
            },
        }


@pytest.mark.asyncio
async def test_a_running_container_tick_updates_only_when_displayed_values_change(
    database: Forwarder,
    test_engine: AsyncEngine,
    admin_with_a_pending_user: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ticks of a running container whose live stats move below display
    granularity put nothing on the wire, and a change the panel renders
    sends exactly one update."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        job = Job(owner_id=None, research_question="Running container", status="running")
        db.add(job)
        await db.commit()
        await db.refresh(job)
        job_id = str(job.id)

    try:
        entry: ContainerEntry = {
            "id": "fake-container-1",
            "name": "openscientist-agent-job-7",
            "status": "running",
            "created": "2026-11-05T09:00:00+00:00",
            "labels": {"openscientist.type": "agent", "openscientist.job_id": job_id},
        }
        fake = _FakeLauncher(entry)
        monkeypatch.setattr(container_dashboard, "_get_launcher_client", lambda: fake)
        _FakeNow.value = datetime(2026, 11, 5, 9, 5, 1, tzinfo=UTC)
        monkeypatch.setattr(container_dashboard, "datetime", _FakeNow)

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
        callback = timer.callback
        assert callback is not None

        async def tick() -> list[WireMessage]:
            wire_recorder.clear()
            with client:
                await callback()
            await drain_outbox(client)
            return [m for m in wire_recorder if m.event == "update" and m.room == client.id]

        _FakeNow.value += timedelta(seconds=0.1)
        sent = await tick()
        assert not sent, (
            f"uptime and CPU below display granularity sent "
            f"{sum(m.wire_bytes for m in sent)} wire bytes"
        )

        _FakeNow.value += timedelta(seconds=0.1)
        sent = await tick()
        assert not sent, (
            f"uptime and CPU below display granularity sent "
            f"{sum(m.wire_bytes for m in sent)} wire bytes"
        )

        _FakeNow.value += timedelta(seconds=1)
        sent = await tick()
        assert len(sent) == 1, f"a displayed uptime change sent {len(sent)} updates"

        fake.cpu_nanos = _CPU_NANOS_55_PERCENT
        sent = await tick()
        assert len(sent) == 1, f"a displayed CPU change sent {len(sent)} updates"

        entry["status"] = "exited"
        sent = await tick()
        assert len(sent) == 1, f"a status change sent {len(sent)} updates"
    finally:
        async with AsyncSession(test_engine) as db:
            await db.execute(delete(Job).where(Job.id == job.id))
            await db.commit()


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
