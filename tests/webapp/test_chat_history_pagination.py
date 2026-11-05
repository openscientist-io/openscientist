"""The Chat tab opens on the newest messages and loads older ones a page at
a time, until nothing older remains.

The page is built in a real signed-in NiceGUI client and read at the moment
NiceGUI sends it (see ``served_page``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import storage, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, JobChatMessage, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import chat as chat_tab
from openscientist.webapp_components.pages.job_detail import job_detail_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, fire_click_twice

PAGE_SIZE = 50
TOTAL = 300


@pytest_asyncio.fixture
async def chat_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A completed job with 300 committed chat messages and a live owner
    session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@chat-paging.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(owner_id=owner.id, research_question="Chat question", status="completed")
        db.add(job)
        await db.flush()
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(TOTAL):
            db.add(
                JobChatMessage(
                    job_id=job.id,
                    role="user" if i % 2 == 0 else "assistant",
                    content=f"Message {i}",
                    created_at=base + timedelta(seconds=i),
                )
            )
        owner_login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(owner_login)
        await db.commit()
    yield {
        "job_id": str(job.id),
        "owner_token": str(owner_login.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(JobChatMessage).where(JobChatMessage.job_id == job.id))
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The page reads the job directory and the latest job from the job manager."""

    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=JobStatus.COMPLETED)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )


def _bubble_texts(client: Client) -> list[str]:
    """The texts of the message bubbles in the message list, top to bottom."""
    with client:
        scroll = next(
            element
            for element in client.elements.values()
            if isinstance(element, ui.scroll_area) and "chat-messages-scroll" in element.classes
        )
        texts: list[str] = []
        for descendant in scroll.descendants():
            if isinstance(descendant, ui.label):
                texts.append(descendant.text)
            elif isinstance(descendant, ui.markdown):
                texts.append(descendant.content)
    return texts


def _load_button(client: Client) -> ui.button:
    for element in client.elements.values():
        if isinstance(element, ui.button) and element.text == "Load earlier messages":
            return element
    raise AssertionError("the Load earlier messages button is missing")


def _load_button_visible(client: Client, button: ui.button) -> bool:
    return not button.is_deleted and "hidden" not in button.classes


async def _until(condition, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, f"{what} within 10 s"
        await asyncio.sleep(0.02)


def _click(client: Client, button: ui.button) -> None:
    """Fire the button's click the way NiceGUI's socket handler does."""
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    storage.request_contextvar.set(listener.request)
    with client, button.parent_slot or nullcontext():
        listener.handler(None)


async def _served(
    database: Forwarder, chat_job: dict[str, str], served_page: ServedPage
) -> tuple[Client, list[str]]:
    client, sent = await served_page(
        chat_job["owner_token"],
        lambda: job_detail_page(chat_job["job_id"]),
        _bubble_texts,
    )
    return client, sent


@pytest.mark.asyncio
async def test_the_first_html_holds_the_newest_messages(
    database: Forwarder,
    chat_job: dict[str, str],
    served_page: ServedPage,
) -> None:
    """With 300 committed messages the first HTML holds messages 251 to 300
    in order, and offers the load-earlier control."""
    client, sent = await _served(database, chat_job, served_page)

    assert sent == [f"Message {i}" for i in range(TOTAL - PAGE_SIZE, TOTAL)]
    assert _load_button_visible(client, _load_button(client))


@pytest.mark.asyncio
async def test_one_press_loads_the_previous_page(
    database: Forwarder,
    chat_job: dict[str, str],
    served_page: ServedPage,
) -> None:
    """One press of the control shows messages 201 to 300 in order, and the
    control stays offered."""
    client, _ = await _served(database, chat_job, served_page)
    button = _load_button(client)
    _click(client, button)
    await _until(lambda: len(_bubble_texts(client)) >= 2 * PAGE_SIZE, "page loaded")

    assert _bubble_texts(client) == [f"Message {i}" for i in range(TOTAL - 2 * PAGE_SIZE, TOTAL)]
    assert _load_button_visible(client, button)


@pytest.mark.asyncio
async def test_the_control_is_gone_when_everything_is_shown(
    database: Forwarder,
    chat_job: dict[str, str],
    served_page: ServedPage,
) -> None:
    """Pressing the control until nothing older remains shows all 300
    messages in order and hides the control."""
    client, _ = await _served(database, chat_job, served_page)
    button = _load_button(client)
    shown = PAGE_SIZE
    for _ in range(TOTAL // PAGE_SIZE):
        if not _load_button_visible(client, button):
            break
        _click(client, button)
        shown = min(shown + PAGE_SIZE, TOTAL)
        await _until(lambda s=shown: len(_bubble_texts(client)) >= s, "page loaded")

    assert _bubble_texts(client) == [f"Message {i}" for i in range(TOTAL)]
    assert not _load_button_visible(client, button)


@pytest.mark.asyncio
async def test_two_simultaneous_presses_load_one_page(
    database: Forwarder,
    chat_job: dict[str, str],
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two presses dispatched together prepend one page, not two."""
    client, _ = await _served(database, chat_job, served_page)
    button = _load_button(client)
    await fire_click_twice(button, client, monkeypatch)

    assert _bubble_texts(client) == [f"Message {i}" for i in range(TOTAL - 2 * PAGE_SIZE, TOTAL)]
    assert _load_button_visible(client, button)


async def _send_and_commit_turn(
    session: AsyncSession, job_id: UUID, message: str, job_dir: Path
) -> str:
    """Commit the turn's user and assistant rows the way the container path does."""
    now = datetime.now(UTC)
    session.add_all(
        [
            JobChatMessage(job_id=job_id, role="user", content=message, created_at=now),
            JobChatMessage(
                job_id=job_id,
                role="assistant",
                content=f"Reply to {message}",
                created_at=now + timedelta(microseconds=1),
            ),
        ]
    )
    await session.commit()
    return f"Reply to {message}"


@pytest.mark.asyncio
async def test_a_send_keeps_the_shown_window_and_appends_the_new_messages(
    database: Forwarder,
    chat_job: dict[str, str],
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a send the newest two messages appear and every message already
    shown stays shown."""
    monkeypatch.setattr(chat_tab, "send_chat_message", _send_and_commit_turn)
    client, _ = await _served(database, chat_job, served_page)

    textarea = next(e for e in client.elements.values() if isinstance(e, ui.textarea))
    send_btn = next(
        e
        for e in client.elements.values()
        if isinstance(e, ui.button) and e.props.get("icon") == "send"
    )
    textarea.value = "What next?"
    _click(client, send_btn)
    await _until(lambda: len(_bubble_texts(client)) >= PAGE_SIZE + 2, "turn committed")

    assert _bubble_texts(client) == [f"Message {i}" for i in range(TOTAL - PAGE_SIZE, TOTAL)] + [
        "What next?",
        "Reply to What next?",
    ]
    assert _load_button_visible(client, _load_button(client))
