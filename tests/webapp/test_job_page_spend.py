"""The job page's spend badge in the stats card and its 2 s poll.

The seed is a running job at iteration 3 of 5 whose committed cost rows sum
to $1.23, so the owner's stats card has a spend badge to compare across
ticks.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import (
    CostRecord,
    IterationSummary,
    Job,
    JobShare,
    Session,
    User,
)
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, WireMessage, drain_outbox


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
async def spend_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A running job with $1.23 of committed cost rows, the owner's session
    and an approved reader with a session and no share."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@spend.test", name="Owner", is_approved=True)
        reader = User(email=f"reader-{uuid4().hex}@spend.test", name="Reader", is_approved=True)
        db.add_all([owner, reader])
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="What keeps the spend badge live",
            status="running",
            current_iteration=3,
            max_iterations=5,
            agent_status="Thinking",
        )
        db.add(job)
        await db.flush()
        db.add_all(
            [
                IterationSummary(
                    job_id=job.id, iteration=1, summary_text="Pass one", strapline="First pass"
                ),
                _cost(job.id, 0.50),
                _cost(job.id, 0.73),
            ]
        )
        owner_login, owner_login_token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        reader_login, reader_login_token = Session.issue(
            user_id=reader.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add_all([owner_login, reader_login])
        await db.commit()
    yield {
        "job_id": str(job.id),
        "question": job.research_question,
        "owner_token": owner_login_token,
        "reader_token": reader_login_token,
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(CostRecord).where(CostRecord.job_id == job.id))
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, reader.id])))
        await db.commit()


@pytest_asyncio.fixture
async def no_cost_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A running job with no committed cost rows and the owner's session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@nocost.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="A job before its first cost row",
            status="running",
            current_iteration=1,
            max_iterations=5,
        )
        db.add(job)
        await db.flush()
        owner_login, owner_login_token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(owner_login)
        await db.commit()
    yield {"job_id": str(job.id), "owner_token": owner_login_token}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(CostRecord).where(CostRecord.job_id == job.id))
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest_asyncio.fixture
async def shared_spend_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A running job with $0.50 of committed cost rows, shared for view with
    the recipient, whose session the recipient holds."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@shared.test", name="Owner", is_approved=True)
        recipient = User(
            email=f"recipient-{uuid4().hex}@shared.test", name="Recipient", is_approved=True
        )
        db.add_all([owner, recipient])
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="A shared job with a spend",
            status="running",
            current_iteration=1,
            max_iterations=5,
        )
        db.add(job)
        await db.flush()
        db.add(_cost(job.id, 0.50))
        db.add(JobShare(job_id=job.id, shared_with_user_id=recipient.id, permission_level="view"))
        recipient_login, recipient_login_token = Session.issue(
            user_id=recipient.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(recipient_login)
        await db.commit()
    yield {"job_id": str(job.id), "recipient_token": recipient_login_token}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(CostRecord).where(CostRecord.job_id == job.id))
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, recipient.id])))
        await db.commit()


@pytest_asyncio.fixture
async def public_spend_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A public running job with committed cost rows and the reader's session,
    the reader holding no share."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@pubspend.test", name="Owner", is_approved=True)
        reader = User(email=f"reader-{uuid4().hex}@pubspend.test", name="Reader", is_approved=True)
        db.add_all([owner, reader])
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="A public job with a spend",
            status="running",
            current_iteration=1,
            max_iterations=5,
            is_public=True,
        )
        db.add(job)
        await db.flush()
        db.add(_cost(job.id, 0.50))
        reader_login, reader_login_token = Session.issue(
            user_id=reader.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(reader_login)
        await db.commit()
    yield {
        "job_id": str(job.id),
        "question": job.research_question,
        "reader_token": reader_login_token,
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(CostRecord).where(CostRecord.job_id == job.id))
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, reader.id])))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The page reads the job directory and the latest job from the job manager."""

    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=JobStatus.RUNNING)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )


_SPEND_BADGE = re.compile(r"^Spend: (.+)$")


def _spend_badge(client: Client) -> str | None:
    """The stats card's spend badge value, or None when the card has none."""
    value: str | None = None
    with client:
        for e in client.elements.values():
            if isinstance(e, ui.label):
                m = _SPEND_BADGE.match(e.text or "")
                if m:
                    value = m.group(1)
    return value


def _spend_label_id(client: Client) -> int | None:
    """The identity of the spend badge label, so a test can tell a refreshed
    badge from the one it replaced."""
    with client:
        for e in client.elements.values():
            if isinstance(e, ui.label) and (e.text or "").startswith("Spend: "):
                return id(e)
    return None


def _iteration_card_ids(client: Client) -> set[int]:
    """The identities of the iteration cards, so a test can tell a rebuilt
    card from an untouched one."""
    with client:
        return {
            id(e)
            for e in client.elements.values()
            if isinstance(e, ui.expansion) and "header" in e.slots
        }


def _timer(client: Client) -> ui.timer:
    with client:
        return next(
            e for e in client.elements.values() if isinstance(e, ui.timer) and e.interval == 2.0
        )


async def _tick(
    client: Client, timer: ui.timer, wire_recorder: list[WireMessage]
) -> list[WireMessage]:
    """Fire the poll once, the way the live timer would, and return the
    update messages it put on the wire."""
    callback = timer.callback
    assert callback is not None
    wire_recorder.clear()
    with client:
        await callback()
    await drain_outbox(client)
    return [m for m in wire_recorder if m.event == "update" and m.room == client.id]


async def _build(served_page: ServedPage, token: str, job_id: str) -> Client:
    client, _ = await served_page(token, lambda: job_detail_page(job_id), lambda c: None)
    return client


@pytest.mark.asyncio
async def test_the_stats_card_shows_the_job_spend(
    database: Forwarder, spend_job: dict[str, str], served_page: ServedPage
) -> None:
    """The owner's stats card carries a spend badge with the job's ledger
    total."""
    client = await _build(served_page, spend_job["owner_token"], spend_job["job_id"])
    assert _spend_badge(client) == "$1.23"


@pytest.mark.asyncio
async def test_an_owner_sees_zero_spend_before_the_first_cost_row(
    database: Forwarder, no_cost_job: dict[str, str], served_page: ServedPage
) -> None:
    """The owner's stats card shows $0.00 while the job has no cost rows yet,
    like the /jobs list does for the same job."""
    client = await _build(served_page, no_cost_job["owner_token"], no_cost_job["job_id"])
    assert _spend_badge(client) == "$0.00"


@pytest.mark.asyncio
async def test_a_view_share_recipient_sees_the_spend_badge(
    database: Forwarder, shared_spend_job: dict[str, str], served_page: ServedPage
) -> None:
    """A recipient of a view share sees the spend badge with the job's
    ledger total."""
    client = await _build(
        served_page, shared_spend_job["recipient_token"], shared_spend_job["job_id"]
    )
    assert _spend_badge(client) == "$0.50"


@pytest.mark.asyncio
async def test_a_new_cost_row_updates_the_spend_badge_without_rebuilding_the_cards(
    database: Forwarder,
    spend_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """A cost row committed between two ticks updates the spend badge and
    touches no iteration card."""
    client = await _build(served_page, spend_job["owner_token"], spend_job["job_id"])
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    card_ids = _iteration_card_ids(client)
    old_spend_id = _spend_label_id(client)
    assert old_spend_id is not None

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        db.add(_cost(UUID(spend_job["job_id"]), 0.77))
        await db.commit()

    sent = await _tick(client, _timer(client), wire_recorder)
    assert sent, "the changed spend badge never reached the wire"
    assert _spend_badge(client) == "$2.00"
    assert _spend_label_id(client) != old_spend_id, "the spend badge was not updated"
    assert _iteration_card_ids(client) == card_ids, "the iteration cards were rebuilt"


@pytest.mark.asyncio
async def test_an_idle_tick_sends_nothing(
    database: Forwarder,
    spend_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
) -> None:
    """A tick over an unchanged job, spend included, puts no update on the
    wire."""
    client = await _build(served_page, spend_job["owner_token"], spend_job["job_id"])
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    sent = await _tick(client, _timer(client), wire_recorder)
    assert not sent, f"an idle tick put {len(sent)} updates on the wire"


@pytest.mark.asyncio
async def test_a_public_reader_sees_no_spend_badge(
    database: Forwarder,
    public_spend_job: dict[str, str],
    served_page: ServedPage,
) -> None:
    """A signed-in reader of a public job with cost rows sees the page but
    no spend badge, because the reader's RLS admits no cost rows."""
    client = await _build(served_page, public_spend_job["reader_token"], public_spend_job["job_id"])
    questions = [
        e.text
        for e in client.elements.values()
        if isinstance(e, ui.label) and e.text == public_spend_job["question"]
    ]
    assert questions, "the public reader never saw the job page"
    assert _spend_badge(client) is None
