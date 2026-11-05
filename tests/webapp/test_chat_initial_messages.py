"""The Chat tab of a finished job carries its committed messages in the first
HTML, for the owner and for a view-only viewer alike.

The page is built in a real signed-in NiceGUI client and read at the moment
NiceGUI sends it (see ``served_page``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, JobChatMessage, JobShare, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage

USER_MESSAGE = "What is the state of the evidence?"
ASSISTANT_MESSAGE = "The evidence so far supports the hypothesis."


@pytest_asyncio.fixture
async def chat_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A completed job whose owner and a view-only viewer both hold live
    sessions, with two committed chat messages."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@chat.test", name="Owner", is_approved=True)
        viewer = User(email=f"viewer-{uuid4().hex}@chat.test", name="Viewer", is_approved=True)
        db.add_all([owner, viewer])
        await db.flush()
        job = Job(owner_id=owner.id, research_question="Chat question", status="completed")
        db.add(job)
        await db.flush()
        db.add(JobChatMessage(job_id=job.id, role="user", content=USER_MESSAGE))
        db.add(JobChatMessage(job_id=job.id, role="assistant", content=ASSISTANT_MESSAGE))
        db.add(JobShare(job_id=job.id, shared_with_user_id=viewer.id, permission_level="view"))
        owner_login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        viewer_login = Session(user_id=viewer.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add_all([owner_login, viewer_login])
        await db.commit()
    yield {
        "job_id": str(job.id),
        "owner_token": str(owner_login.id),
        "viewer_token": str(viewer_login.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(JobChatMessage).where(JobChatMessage.job_id == job.id))
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, viewer.id])))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The page reads the job directory and the latest job from the job manager."""

    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=JobStatus.COMPLETED)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )


def _sent_bubbles(client: Client) -> list[tuple[str, str]]:
    """The message bubbles inside ``client``, as (role, text) pairs."""
    with client:
        bubbles: list[tuple[str, str]] = []
        for element in client.elements.values():
            if not isinstance(element, ui.element):
                continue
            if "chat-bubble-user" in element.classes:
                for child in element.descendants():
                    if isinstance(child, ui.label):
                        bubbles.append(("user", child.text))
            elif "chat-bubble-assistant" in element.classes:
                for child in element.descendants():
                    if isinstance(child, ui.markdown):
                        bubbles.append(("assistant", child.content))
        return bubbles


@pytest.mark.asyncio
async def test_the_first_html_already_holds_the_chat_messages(
    database: Forwarder,
    chat_job: dict[str, str],
    served_page: ServedPage,
) -> None:
    """The owner's committed messages are rendered before NiceGUI sends the page."""
    client, sent = await served_page(
        chat_job["owner_token"],
        lambda: job_detail_page(chat_job["job_id"]),
        _sent_bubbles,
    )
    assert ("user", USER_MESSAGE) in sent
    assert ("assistant", ASSISTANT_MESSAGE) in sent
    assert _sent_bubbles(client) == sent


@pytest.mark.asyncio
async def test_a_view_only_viewer_sees_the_chat_messages_in_the_first_html(
    database: Forwarder,
    chat_job: dict[str, str],
    served_page: ServedPage,
) -> None:
    """The viewer's first HTML holds the bubbles and the view-only note, and
    no input area."""
    client, sent = await served_page(
        chat_job["viewer_token"],
        lambda: job_detail_page(chat_job["job_id"]),
        _sent_bubbles,
    )
    assert ("user", USER_MESSAGE) in sent
    assert ("assistant", ASSISTANT_MESSAGE) in sent
    assert not any(isinstance(e, ui.textarea) for e in client.elements.values())
