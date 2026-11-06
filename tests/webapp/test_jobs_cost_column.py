"""The Cost column of the /jobs tables, read at the moment NiceGUI sends the
page and at the 5 s tick (see ``served_page``).

The seed is an owner with one job whose committed cost rows sum to $1.23 and
one with no cost rows, plus a sharer with a $0.89 job shared with the owner.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import CostRecord, Job, JobShare, Session, User
from openscientist.webapp_components.pages.jobs_list import jobs_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage


def _cost(job_id: UUID, cost_usd: float) -> CostRecord:
    return CostRecord(
        job_id=job_id,
        operation_type="analysis",
        provider="anthropic",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=cost_usd,
    )


@pytest_asyncio.fixture
async def cost_owner(test_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    """The seeded owner, sharer and jobs, all committed, with the owner's
    live session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@cost.test", name="Owner", is_approved=True)
        sharer = User(email=f"sharer-{uuid4().hex}@cost.test", name="Sharer", is_approved=True)
        db.add_all([owner, sharer])
        await db.flush()
        costed = Job(owner_id=owner.id, research_question="Costed question", status="completed")
        plain = Job(owner_id=owner.id, research_question="Plain question", status="running")
        shared = Job(owner_id=sharer.id, research_question="Shared question", status="completed")
        db.add_all([costed, plain, shared])
        await db.flush()
        db.add_all(
            [
                _cost(costed.id, 0.50),
                _cost(costed.id, 0.73),
                _cost(shared.id, 0.89),
            ]
        )
        db.add(JobShare(job_id=shared.id, shared_with_user_id=owner.id, permission_level="view"))
        owner_login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(owner_login)
        await db.commit()
    yield {
        "costed": str(costed.id),
        "plain": str(plain.id),
        "shared": str(shared.id),
        "owner_token": str(owner_login.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id.in_([costed.id, plain.id, shared.id])))
        await db.execute(delete(User).where(User.id.in_([owner.id, sharer.id])))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page hands the job manager only to its delete dialog."""
    monkeypatch.setattr(web_app._state, "job_manager", object())


def _cost_by_job_id(client: Client, *, shared: bool) -> dict[str, str]:
    """The Cost cell of each row of the named table, keyed by job id."""
    tables = [e for e in client.elements.values() if isinstance(e, ui.table)]
    table = next(t for t in tables if any(c["name"] == "owner" for c in t.columns) is shared)
    return {row["job_id"]: row["cost"] for row in table.rows}


async def _open(served_page: ServedPage, token: str) -> Client:
    client, _ = await served_page(token, jobs_page, lambda client: None)
    return client


def _refresh_callbacks(client: Client) -> list[Any]:
    callbacks = []
    for element in client.elements.values():
        if isinstance(element, ui.timer) and element.interval == 5.0:
            assert element.callback is not None
            callbacks.append(element.callback)
    return callbacks


@pytest.mark.asyncio
async def test_the_cost_column_shows_each_job_spend(
    database: Forwarder, cost_owner: dict[str, Any], served_page: ServedPage
) -> None:
    """The My Jobs table carries a Cost column, $1.23 for the job with cost
    rows and $0.00 for the job without any."""
    client = await _open(served_page, cost_owner["owner_token"])
    tables = [e for e in client.elements.values() if isinstance(e, ui.table)]
    owned = next(t for t in tables if any(c["name"] == "owner" for c in t.columns) is False)
    assert {"name": "cost", "label": "Cost", "field": "cost", "align": "center"} in owned.columns
    costs = _cost_by_job_id(client, shared=False)
    assert costs[cost_owner["costed"]] == "$1.23"
    assert costs[cost_owner["plain"]] == "$0.00"


@pytest.mark.asyncio
async def test_a_share_recipient_sees_the_cost_column(
    database: Forwarder, cost_owner: dict[str, Any], served_page: ServedPage
) -> None:
    """The Shared With Me table shows the shared job's spend to the
    recipient."""
    client = await _open(served_page, cost_owner["owner_token"])
    costs = _cost_by_job_id(client, shared=True)
    assert costs[cost_owner["shared"]] == "$0.89"


@pytest.mark.asyncio
async def test_a_new_cost_row_reaches_the_next_tick(
    database: Forwarder,
    cost_owner: dict[str, Any],
    served_page: ServedPage,
    test_engine: AsyncEngine,
) -> None:
    """A cost row committed between two ticks shows its new total on the next
    tick."""
    client = await _open(served_page, cost_owner["owner_token"])
    assert _cost_by_job_id(client, shared=False)[cost_owner["costed"]] == "$1.23"

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        db.add(_cost(UUID(cost_owner["costed"]), 0.77))
        await db.commit()

    with client:
        for refresh in _refresh_callbacks(client):
            await refresh()

    assert _cost_by_job_id(client, shared=False)[cost_owner["costed"]] == "$2.00"
