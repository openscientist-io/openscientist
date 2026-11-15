"""The /admin page's read handlers in a real signed-in NiceGUI client, fired
after the admin lost the admin role or the session row while the page stayed open."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import events, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, Job, Session, User
from openscientist.webapp_components.components.common import render_user_search
from openscientist.webapp_components.pages import admin as admin_page
from openscientist.webapp_components.utils import container_dashboard
from openscientist.webapp_components.utils.container_dashboard import (
    DashboardData,
    JobContainerGroup,
)
from openscientist.webapp_components.utils.permissions import AdminAccess
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenPage

REVOCATIONS = ["demoted", "session_deleted"]


@dataclass(frozen=True)
class AdminRecords:
    admin: User
    token: str
    owner: User
    orphan: Job
    private: Job


@pytest_asyncio.fixture
async def admin_records(test_engine: AsyncEngine) -> AsyncIterator[AdminRecords]:
    """A signed-in admin, another user with a private running job, and an orphaned job."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        admin = User(email=f"admin-{uuid4().hex}@recheck.test", name="Admin", is_approved=True)
        owner = User(email=f"zx-{uuid4().hex}@recheck.test", name="Private Owner")
        session.add_all([admin, owner])
        await session.flush()
        session.add(Administrator(user_id=admin.id))
        login, token = Session.issue(
            user_id=admin.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        orphan = Job(owner_id=None, research_question="Initial orphan", status="completed")
        private = Job(owner_id=owner.id, research_question="Private research", status="running")
        session.add_all([login, orphan, private])
        await session.commit()
    yield AdminRecords(admin, token, owner, orphan, private)
    async with AsyncSession(test_engine) as session:
        await session.execute(delete(Job).where(Job.id.in_([orphan.id, private.id])))
        await session.execute(delete(User).where(User.id.in_([admin.id, owner.id])))
        await session.commit()


async def _revoke(test_engine: AsyncEngine, admin: User, revocation: str) -> None:
    """Take the admin role or the login session away from ``admin``."""
    model = Administrator if revocation == "demoted" else Session
    async with AsyncSession(test_engine) as session:
        await session.execute(delete(model).where(model.user_id == admin.id))
        await session.commit()


def _texts(client: Client) -> str:
    """Every label text and table row the client holds, as one string."""
    parts: list[str] = []
    for element in client.elements.values():
        if isinstance(element, ui.label):
            parts.append(element.text)
        if isinstance(element, ui.table):
            parts.extend(repr(row) for row in element.rows)
    return "\n".join(parts)


async def _type(search_input: ui.input, client: Client, query: str) -> None:
    """Type ``query`` into ``search_input`` and await its change handlers to the end.

    Outside a started app NiceGUI defers the handlers it schedules, so the
    test runs them itself."""
    previous = search_input.value
    search_input.set_value(query)
    arguments = events.ValueChangeEventArguments(
        sender=search_input, client=client, value=query, previous_value=previous
    )
    with search_input.parent_slot or client:
        for handler in search_input._change_handlers:
            run: Callable[..., object] = handler
            result = run(arguments)
            if inspect.isawaitable(result):
                await result


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", REVOCATIONS)
async def test_the_orphaned_job_search_reads_nothing_once_admin_rights_are_gone(
    database: Forwarder,
    test_engine: AsyncEngine,
    admin_records: AdminRecords,
    signed_in_page: OpenPage,
    revocation: str,
) -> None:
    holder: list[ui.column] = []

    async def build() -> None:
        holder.append(ui.column())

    client = await signed_in_page(admin_records.token, build)
    with client:
        await admin_page.load_orphaned_jobs(holder[0], search_query="Initial orphan")
    assert "Initial orphan" in _texts(client)

    await _revoke(test_engine, admin_records.admin, revocation)
    async with AsyncSession(test_engine) as session:
        orphan = await session.get(Job, admin_records.orphan.id)
        assert orphan is not None
        orphan.research_question = "Fresh orphan secret"
        await session.commit()
    with client:
        await admin_page.load_orphaned_jobs(holder[0], search_query="Fresh orphan")

    assert "Fresh orphan secret" not in _texts(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", REVOCATIONS)
async def test_the_assign_user_search_reads_nothing_once_admin_rights_are_gone(
    database: Forwarder,
    test_engine: AsyncEngine,
    admin_records: AdminRecords,
    signed_in_page: OpenPage,
    revocation: str,
) -> None:
    built: list[ui.input] = []

    async def on_select(user_data: dict[str, object]) -> None:
        return None

    async def build() -> None:
        search_input, _ = await render_user_search(on_select=on_select)
        built.append(search_input)

    client = await signed_in_page(admin_records.token, build)
    await _type(built[0], client, "Admin")
    assert admin_records.admin.email in _texts(client)

    await _revoke(test_engine, admin_records.admin, revocation)
    await _type(built[0], client, "zx-")

    assert admin_records.owner.email not in _texts(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", REVOCATIONS)
async def test_the_containers_tick_reads_nothing_and_stops_once_admin_rights_are_gone(
    database: Forwarder,
    test_engine: AsyncEngine,
    admin_records: AdminRecords,
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
    revocation: str,
) -> None:
    payloads = [DashboardData(docker_available=True)]

    async def collect(access: AdminAccess, include_stats: bool = True) -> DashboardData:
        return payloads[0]

    monkeypatch.setattr(container_dashboard, "collect_dashboard_data", collect)
    monkeypatch.setattr(
        "openscientist.webapp_components.utils.client_guard.go_to_login", lambda: None
    )

    async def build() -> None:
        await admin_page.render_containers_panel(str(admin_records.admin.id))

    client = await signed_in_page(admin_records.token, build)
    tick = next(
        element
        for element in client.elements.values()
        if isinstance(element, ui.timer) and element.interval == 3.0
    )
    assert tick.callback is not None

    await _revoke(test_engine, admin_records.admin, revocation)
    payloads[0] = DashboardData(
        docker_available=True,
        job_groups=[
            JobContainerGroup(
                job_id=str(admin_records.private.id),
                title="Fresh dashboard secret",
                status="running",
                owner_email=admin_records.owner.email,
                current_iteration=1,
                max_iterations=5,
            )
        ],
    )
    with client:
        await tick.callback()

    assert "Fresh dashboard secret" not in _texts(client)
    assert admin_records.owner.email not in _texts(client)
    assert tick.active is False
