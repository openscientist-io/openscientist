"""The feedback panel of a job page in a signed-in NiceGUI client.

A job in ``awaiting_feedback`` offers Submit & Continue and Continue Without
Feedback, both bound to one handler. Two clicks racing on the event loop must
store the feedback once and resume the job once, and a failed save must leave
the buttons usable.

The page is built the way NiceGUI serves it (see ``served_page``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, storage, ui
from nicegui.client import Client
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import FeedbackHistory, Job, Session, User
from openscientist.job.types import JobStatus
from openscientist.job_manager import JobManager
from openscientist.knowledge_state import KnowledgeState
from openscientist.webapp_components.pages.job_detail import job_detail_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, fire_click_twice

FEEDBACK = "focus on the pathway"


@pytest_asyncio.fixture
async def feedback_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An owner with a live session and a job paused after its second
    iteration, committed in ``awaiting_feedback``."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@feedback.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="Feedback question",
            status="awaiting_feedback",
            current_iteration=2,
            max_iterations=5,
        )
        db.add(job)
        await db.flush()
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {
        "job_id": str(job.id),
        "token": str(login.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(FeedbackHistory).where(FeedbackHistory.job_id == job.id))
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The page reads the job directory and the latest job from the job manager."""
    monkeypatch.setattr(web_app._state, "job_manager", JobManager(jobs_dir=tmp_path))


async def _served(served_page: ServedPage, job: dict[str, str]) -> Client:
    client, _sent = await served_page(
        job["token"],
        lambda: job_detail_page(job["job_id"]),
        lambda _client: None,
    )
    return client


def _button(client: Client, text: str) -> ui.button:
    with client:
        return next(
            e for e in client.elements.values() if isinstance(e, ui.button) and e.text == text
        )


def _feedback_input(client: Client) -> ui.textarea:
    with client:
        return next(
            e
            for e in client.elements.values()
            if isinstance(e, ui.textarea) and e.label == "Your Feedback (optional)"
        )


async def _fire_once(button: ui.button, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire the button's click once the way NiceGUI's socket handler does,
    so the handler task runs on the event loop."""
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    handler = listener.handler
    scheduled: list[Awaitable[None]] = []

    def _capture(awaitable: Awaitable[None], *, name: str = "") -> None:
        scheduled.append(awaitable)

    monkeypatch.setattr(background_tasks, "create_or_defer", _capture)
    storage.request_contextvar.set(listener.request)
    with client:
        with button.parent_slot or nullcontext():
            handler(None)
    assert len(scheduled) == 1
    await asyncio.gather(*scheduled)


@pytest.mark.asyncio
async def test_a_double_click_submits_the_feedback_once(
    database: Forwarder,
    test_engine: AsyncEngine,
    feedback_job: dict[str, str],
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clicks racing on the event loop store one feedback row, resume the
    job once, and never warn that the job is no longer waiting."""
    job_id = UUID(feedback_job["job_id"])
    client = await _served(served_page, feedback_job)
    button = _button(client, "Submit & Continue")
    with client:
        _feedback_input(client).value = FEEDBACK

    await fire_click_twice(button, client, monkeypatch)

    async with AsyncSession(test_engine) as db:
        rows = (
            (await db.execute(select(FeedbackHistory).where(FeedbackHistory.job_id == job_id)))
            .scalars()
            .all()
        )
        status = (await db.execute(select(Job.status).where(Job.id == job_id))).scalar_one()
    assert len(rows) == 1, f"expected one feedback row, got {len(rows)}"
    assert rows[0].feedback_text == FEEDBACK
    assert status == JobStatus.RUNNING.value
    notifies = [data for _target, kind, data in client.outbox.messages if kind == "notify"]
    assert not any(
        data.get("message") == "This job is no longer waiting for feedback." for data in notifies
    )
    assert sum(1 for data in notifies if data.get("message") == "Continuing to next iteration") == 1


@pytest.mark.asyncio
async def test_a_failed_save_leaves_the_feedback_buttons_usable(
    database: Forwarder,
    test_engine: AsyncEngine,
    feedback_job: dict[str, str],
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A feedback save that fails re-enables both buttons, stores nothing,
    and leaves the job waiting for its feedback."""

    async def _fail_save(self, job_id: str, session: AsyncSession | None = None) -> None:
        raise RuntimeError("simulated feedback save failure")

    monkeypatch.setattr(KnowledgeState, "save_to_database", _fail_save)

    job_id = UUID(feedback_job["job_id"])
    client = await _served(served_page, feedback_job)
    button = _button(client, "Submit & Continue")
    continue_button = _button(client, "Continue Without Feedback")
    with client:
        _feedback_input(client).value = FEEDBACK

    await _fire_once(button, client, monkeypatch)

    assert button.enabled is True
    assert continue_button.enabled is True

    async with AsyncSession(test_engine) as db:
        stored = (
            await db.execute(
                select(func.count())
                .select_from(FeedbackHistory)
                .where(FeedbackHistory.job_id == job_id)
            )
        ).scalar_one()
        status = (await db.execute(select(Job.status).where(Job.id == job_id))).scalar_one()
    assert stored == 0
    assert status == JobStatus.AWAITING_FEEDBACK.value
