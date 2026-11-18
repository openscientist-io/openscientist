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
from openscientist.database.models.user_feedback import FEEDBACK_MAX_LENGTH
from openscientist.webapp_components.components import feedback as feedback_component
from openscientist.webapp_components.components.feedback import render_feedback_button
from openscientist.webapp_components.pages import feedback_panel
from openscientist.webapp_components.pages.feedback_panel import render_feedback_panel
from openscientist.webapp_components.utils.permissions import AdminAccess
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


@pytest.mark.asyncio
async def test_an_overlong_message_is_not_sent(people: _People, test_engine: AsyncEngine) -> None:
    async with user_simulation(root=render_feedback_button) as browser:
        browser.http_client.cookies.set("session_token", people.sender_token)
        await browser.open("/")
        browser.find(kind=ui.button, content="Feedback").click()
        browser.find(kind=ui.textarea).type("x" * (FEEDBACK_MAX_LENGTH + 1))
        browser.find(kind=ui.button, content="Send").click()
        await browser.should_see(f"Please keep the message under {FEEDBACK_MAX_LENGTH} characters")

    assert await _stored(test_engine) == []


@pytest.mark.asyncio
async def test_a_sender_whose_session_ended_is_asked_to_sign_in_again(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with user_simulation(root=render_feedback_button) as browser:
        browser.http_client.cookies.set("session_token", people.sender_token)
        await browser.open("/")
        browser.find(kind=ui.button, content="Feedback").click()
        browser.find(kind=ui.textarea).type("Still there?")
        async with AsyncSession(test_engine) as db:
            await db.execute(delete(Session).where(Session.user_id == people.sender))
            await db.commit()
        browser.find(kind=ui.button, content="Send").click()
        await browser.should_see("Your session has ended", retries=50)

    assert await _stored(test_engine) == []


@pytest.mark.asyncio
async def test_a_double_click_on_send_stores_the_message_once(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with user_simulation(root=render_feedback_button) as browser:
        browser.http_client.cookies.set("session_token", people.sender_token)
        await browser.open("/")
        browser.find(kind=ui.button, content="Feedback").click()
        browser.find(kind=ui.textarea).type("Only once please")
        # Both clicks land before the first handler runs and disables the button.
        browser.find(kind=ui.button, content="Send").click()
        browser.find(kind=ui.button, content="Send").click()
        await browser.should_see("Thank you", retries=50)

    assert [stored.message for stored in await _stored(test_engine)] == ["Only once please"]


@pytest.mark.asyncio
async def test_a_database_outage_while_sending_tells_the_sender_it_failed(
    people: _People,
    test_engine: AsyncEngine,
    database: Forwarder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revalidate = feedback_component.revalidate_session

    async def revalidate_then_outage() -> dict[str, object] | None:
        user_info = await revalidate()
        database.down()
        return user_info

    monkeypatch.setattr(feedback_component, "revalidate_session", revalidate_then_outage)
    async with user_simulation(root=render_feedback_button) as browser:
        browser.http_client.cookies.set("session_token", people.sender_token)
        await browser.open("/")
        browser.find(kind=ui.button, content="Feedback").click()
        browser.find(kind=ui.textarea).type("Lost in the outage")
        browser.find(kind=ui.button, content="Send").click()
        await browser.should_see("Your message could not be sent", retries=50)

    assert await _stored(test_engine) == []


def _outage_after_admin_check(database: Forwarder, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Bring the database down right after each admin check while the returned switch holds True."""
    armed = [False]
    check = feedback_panel.admin_access

    async def check_then_outage() -> AdminAccess | None:
        access = await check()
        if armed[0]:
            database.down()
        return access

    monkeypatch.setattr(feedback_panel, "admin_access", check_then_outage)
    return armed


@pytest.mark.asyncio
async def test_the_panel_shows_an_error_when_feedback_cannot_be_loaded(
    people: _People, database: Forwarder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _outage_after_admin_check(database, monkeypatch)[0] = True
    async with user_simulation(root=partial(render_feedback_panel, str(people.admin))) as browser:
        browser.http_client.cookies.set("session_token", people.admin_token)
        await browser.open("/")
        await browser.should_see("Failed to load feedback", retries=50)


@pytest.mark.asyncio
async def test_a_failed_mark_keeps_the_message_unread_and_says_so(
    people: _People,
    test_engine: AsyncEngine,
    database: Forwarder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(UserFeedback(user_id=people.sender, message="Please fix the export"))
        await db.commit()
    armed = _outage_after_admin_check(database, monkeypatch)

    async with user_simulation(root=partial(render_feedback_panel, str(people.admin))) as browser:
        browser.http_client.cookies.set("session_token", people.admin_token)
        await browser.open("/")
        await browser.should_see("Please fix the export", retries=50)
        armed[0] = True
        browser.find(kind=ui.button, content="Mark as read").click()
        await browser.should_see("Failed to update the message", retries=50)

    [stored] = await _stored(test_engine)
    assert stored.read_at is None


@pytest.mark.asyncio
async def test_marking_a_message_deleted_meanwhile_says_it_no_longer_exists(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(UserFeedback(user_id=people.sender, message="Soon gone"))
        await db.commit()

    async with user_simulation(root=partial(render_feedback_panel, str(people.admin))) as browser:
        browser.http_client.cookies.set("session_token", people.admin_token)
        await browser.open("/")
        await browser.should_see("Soon gone", retries=50)
        async with AsyncSession(test_engine) as db:
            await db.execute(delete(UserFeedback))
            await db.commit()
        browser.find(kind=ui.button, content="Mark as read").click()
        await browser.should_see("This message no longer exists", retries=50)
        await browser.should_see("No feedback yet.")


@pytest.mark.asyncio
async def test_a_message_from_a_deleted_account_names_no_sender(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(UserFeedback(user_id=None, message="Sent before leaving"))
        await db.commit()

    async with user_simulation(root=partial(render_feedback_panel, str(people.admin))) as browser:
        browser.http_client.cookies.set("session_token", people.admin_token)
        await browser.open("/")
        await browser.should_see("Sent before leaving", retries=50)
        await browser.should_see("Deleted user")


@pytest.mark.asyncio
async def test_the_unread_count_follows_marks(people: _People, test_engine: AsyncEngine) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(UserFeedback(user_id=people.sender, message="Count me"))
        await db.commit()
    counts: list[int] = []

    panel = partial(render_feedback_panel, str(people.admin), counts.append)
    async with user_simulation(root=panel) as browser:
        browser.http_client.cookies.set("session_token", people.admin_token)
        await browser.open("/")
        await browser.should_see("1 unread of 1", retries=50)
        browser.find(kind=ui.button, content="Mark as read").click()
        await browser.should_see("0 unread of 1", retries=50)

    assert counts == [1, 0]


@pytest.mark.asyncio
async def test_a_non_admin_sees_no_feedback(people: _People, test_engine: AsyncEngine) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(UserFeedback(user_id=people.sender, message="For admins only"))
        await db.commit()

    async with user_simulation(root=partial(render_feedback_panel, str(people.sender))) as browser:
        browser.http_client.cookies.set("session_token", people.sender_token)
        await browser.open("/")
        await browser.should_see("Your admin session is no longer valid", retries=50)
        await browser.should_not_see("For admins only")


@pytest.mark.asyncio
async def test_a_demoted_admin_cannot_mark_feedback_read(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(UserFeedback(user_id=people.sender, message="Read me later"))
        await db.commit()

    async with user_simulation(root=partial(render_feedback_panel, str(people.admin))) as browser:
        browser.http_client.cookies.set("session_token", people.admin_token)
        await browser.open("/")
        await browser.should_see("Read me later", retries=50)
        async with AsyncSession(test_engine) as db:
            await db.execute(delete(Administrator).where(Administrator.user_id == people.admin))
            await db.commit()
        browser.find(kind=ui.button, content="Mark as read").click()
        await browser.should_see("Your admin session is no longer valid", retries=50)

    [stored] = await _stored(test_engine)
    assert stored.read_at is None
