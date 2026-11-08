"""Action-time permission rechecks inside a real signed-in NiceGUI page.

The page's request carries only the login cookie. The page is built behind
``require_auth`` in a NiceGUI client of its own, and the real handlers then
fire there, so the recheck reads the token the way a deployed page does.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, events, storage, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, Job, Session, User
from openscientist.webapp_components.components.dialogs import PreparedDialog, render_share_dialog
from openscientist.webapp_components.pages import admin as admin_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenPage


@pytest_asyncio.fixture
async def signed_in_admin(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, Job, str, User]]:
    """An admin who owns a private job, their live session's token, and an
    unapproved user, all committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@page.test", name="Page Owner", is_approved=True)
        pending = User(email=f"pending-{uuid4().hex}@page.test", name="Pending", is_approved=False)
        db.add_all([owner, pending])
        await db.flush()
        db.add(Administrator(user_id=owner.id))
        job = Job(owner_id=owner.id, research_question="Page recheck", status="completed")
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add_all([job, login])
        await db.commit()
    yield owner, job, str(login.id), pending
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, pending.id])))
        await db.commit()


EventHandler = Callable[..., Awaitable[None] | None]


async def _fire(handler: EventHandler, arguments: events.UiEventArguments) -> None:
    """Run a page handler the way NiceGUI does, in its sender's slot, and await it to the end."""
    with arguments.sender.parent_slot or nullcontext():
        result = handler(arguments)
        if inspect.isawaitable(result):
            await result


@pytest.mark.asyncio
async def test_owner_changes_visibility_from_a_signed_in_page(
    database: Forwarder,
    test_engine: AsyncEngine,
    signed_in_admin: tuple[User, Job, str, User],
    signed_in_page: OpenPage,
) -> None:
    """Flipping the share dialog's public switch on the owner's own page makes the job public."""
    _, job, token, _ = signed_in_admin
    built: list[PreparedDialog] = []

    async def build() -> None:
        built.append(render_share_dialog(str(job.id)))

    client = await signed_in_page(token, build)
    switch = next(e for e in built[0].dialog.descendants() if isinstance(e, ui.switch))
    with client:
        for handler in switch._change_handlers:
            await _fire(
                handler,
                events.ValueChangeEventArguments(
                    sender=switch, client=client, value=True, previous_value=False
                ),
            )

    async with AsyncSession(test_engine) as db:
        row = await db.get(Job, job.id)
        assert row is not None
        assert row.is_public is True


@pytest.mark.asyncio
async def test_admin_approves_a_user_from_a_signed_in_page(
    database: Forwarder,
    test_engine: AsyncEngine,
    signed_in_admin: tuple[User, Job, str, User],
    signed_in_page: OpenPage,
) -> None:
    """An admin action fired from the admin's own page passes the admin recheck."""
    _, _, token, pending = signed_in_admin
    built: list[ui.button] = []

    async def reload_users() -> None:
        return None

    async def approve(_: events.ClickEventArguments) -> None:
        event = type("Event", (), {"args": {"id": str(pending.id)}})()
        await admin_page._handle_approval_update(event, is_approved=True, reload_users=reload_users)

    async def build() -> None:
        built.append(ui.button("Approve", on_click=approve))

    client = await signed_in_page(token, build)
    button = built[0]
    with client:
        await _fire(approve, events.ClickEventArguments(sender=button, client=client))

    async with AsyncSession(test_engine) as db:
        row = await db.get(User, pending.id)
        assert row is not None
        assert row.is_approved is True


class _RecordingJobManager:
    """A job manager that records its create_job calls without creating jobs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_job(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


async def _fire_click(button: ui.button, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire the button's click the way NiceGUI's socket handler does and await it.

    A deployment schedules page handlers on the app's background tasks, which
    only exist while ui.run owns the loop, so the test captures the scheduled
    coroutine and awaits it here."""
    scheduled: list[Awaitable[None]] = []

    def _capture(awaitable: Awaitable[None], *, name: str = "") -> None:
        scheduled.append(awaitable)

    monkeypatch.setattr(background_tasks, "create_or_defer", _capture)
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    handler = listener.handler
    storage.request_contextvar.set(listener.request)
    with client:
        with button.parent_slot or nullcontext():
            handler(None)
    assert len(scheduled) == 1
    await scheduled[0]


@pytest.mark.asyncio
async def test_submit_job_is_refused_once_the_user_is_demoted(
    database: Forwarder,
    test_engine: AsyncEngine,
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user demoted while their /new tab stays open is refused at submit
    time, and no job is created."""
    from openscientist import web_app
    from openscientist.webapp_components.pages import new_job

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"demoted-{uuid4().hex}@page.test", name="Demoted", is_approved=True)
        db.add(user)
        await db.flush()
        login = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
        token = str(login.id)

    try:
        recorder = _RecordingJobManager()
        monkeypatch.setattr(web_app, "get_job_manager", lambda: recorder)
        monkeypatch.setattr(new_job, "check_provider_config", lambda: (True, "Mock", None))

        client = await signed_in_page(token, new_job.new_job_page)
        question = next(e for e in client.layout.descendants() if isinstance(e, ui.textarea))
        button = next(
            e
            for e in client.layout.descendants()
            if isinstance(e, ui.button) and e.text == "Start Discovery"
        )

        # An admin demotes the user while the tab stays open.
        storage.request_contextvar.set(None)
        ok, message = await admin_page.set_user_approval_status(user.id, False)
        assert ok, message

        question.value = "Why do corals bleach when the water warms?"
        await _fire_click(button, client, monkeypatch)

        assert recorder.calls == []
        messages = [data for _target, kind, data in client.outbox.messages if kind == "notify"]
        assert any(
            data.get("message") == "Your account is pending administrator approval."
            for data in messages
        )
    finally:
        async with AsyncSession(test_engine) as db:
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()
