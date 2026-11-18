"""Sending feedback from the jobs page and reading it on the admin page."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.testing import user_simulation
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, Session, User, UserFeedback
from openscientist.webapp_components.components.feedback import render_feedback_button
from openscientist.webapp_components.pages.feedback_panel import render_feedback_panel
from tests.db_outage import Forwarder


@dataclass(frozen=True)
class _People:
    sender: UUID
    sender_token: str
    admin: UUID
    admin_token: str


@pytest_asyncio.fixture
async def people(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[_People]:
    """A signed-in user and a signed-in admin, with no feedback stored yet."""
    expires_at = datetime.now(UTC) + timedelta(days=1)
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        await db.execute(delete(UserFeedback))
        sender = User(email=f"sender-{uuid4().hex}@feedback.test", name="Grace Sender")
        admin = User(email=f"admin-{uuid4().hex}@feedback.test", name="Admin", is_approved=True)
        db.add_all([sender, admin])
        await db.flush()
        sender_login, sender_token = Session.issue(user_id=sender.id, expires_at=expires_at)
        admin_login, admin_token = Session.issue(user_id=admin.id, expires_at=expires_at)
        db.add_all([Administrator(user_id=admin.id), sender_login, admin_login])
        await db.commit()
    yield _People(sender.id, sender_token, admin.id, admin_token)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(UserFeedback))
        await db.execute(delete(User).where(User.id.in_([sender.id, admin.id])))
        await db.commit()


async def _stored(test_engine: AsyncEngine) -> list[UserFeedback]:
    async with AsyncSession(test_engine) as db:
        return list((await db.execute(select(UserFeedback))).scalars())


@pytest.mark.asyncio
async def test_a_user_sends_feedback_from_the_dialog(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with user_simulation(root=render_feedback_button) as browser:
        browser.http_client.cookies.set("session_token", people.sender_token)
        await browser.open("/")
        browser.find(kind=ui.button, content="Feedback").click()
        await browser.should_see("Send feedback")
        browser.find(kind=ui.textarea).type("  The plots are too small to read  ")
        browser.find(kind=ui.button, content="Send").click()
        await browser.should_see("Thank you", retries=50)

    [stored] = await _stored(test_engine)
    assert stored.user_id == people.sender
    assert stored.message == "The plots are too small to read"


@pytest.mark.asyncio
async def test_an_empty_message_is_not_sent(people: _People, test_engine: AsyncEngine) -> None:
    async with user_simulation(root=render_feedback_button) as browser:
        browser.http_client.cookies.set("session_token", people.sender_token)
        await browser.open("/")
        browser.find(kind=ui.button, content="Feedback").click()
        browser.find(kind=ui.textarea).type("   ")
        browser.find(kind=ui.button, content="Send").click()
        await browser.should_see("Please write a few words first")

    assert await _stored(test_engine) == []


@pytest.mark.asyncio
async def test_a_sender_at_the_daily_limit_is_told_to_come_back_tomorrow(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with AsyncSession(test_engine) as db:
        db.add_all(UserFeedback(user_id=people.sender, message=f"Earlier {n}") for n in range(10))
        await db.commit()

    async with user_simulation(root=render_feedback_button) as browser:
        browser.http_client.cookies.set("session_token", people.sender_token)
        await browser.open("/")
        browser.find(kind=ui.button, content="Feedback").click()
        browser.find(kind=ui.textarea).type("One more thing")
        browser.find(kind=ui.button, content="Send").click()
        await browser.should_see("Please try again tomorrow", retries=50)

    assert len(await _stored(test_engine)) == 10


@pytest.mark.asyncio
async def test_an_admin_reads_feedback_and_marks_it_read(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(UserFeedback(user_id=people.sender, message="Line one\nLine two"))
        await db.commit()

    async with user_simulation(root=partial(render_feedback_panel, str(people.admin))) as browser:
        browser.http_client.cookies.set("session_token", people.admin_token)
        await browser.open("/")
        await browser.should_see("Line one\nLine two", retries=50)
        await browser.should_see("Grace Sender")
        await browser.should_see("New")
        browser.find(kind=ui.button, content="Mark as read").click()
        await browser.should_see(kind=ui.button, content="Mark as unread", retries=50)

    [stored] = await _stored(test_engine)
    assert stored.read_at is not None
    assert stored.read_by == people.admin
