"""The jobs list opens the delete dialog only for the caller's own job, so a
forged delete event reveals nothing about another user's job."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, ui
from nicegui.client import Client
from nicegui.testing import User as SimulatedBrowser
from nicegui.testing import user_simulation
from nicegui.testing.user_interaction import UserInteraction
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, Session, User
from openscientist.job_manager import JobManager
from openscientist.webapp_components.pages.jobs_list import jobs_page
from tests.db_outage import Forwarder

ACTIVE_WARNING = "This job is currently active and will be cancelled before deletion."


@dataclass(frozen=True)
class DeleteWorld:
    """A stranger and an owner, each signed in, and the owner's running job."""

    stranger_token: str
    owner_token: str
    running_job_id: str


@pytest_asyncio.fixture
async def delete_world(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[DeleteWorld]:
    """The committed rows of one delete-dialog scenario, removed afterwards."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        stranger = User(
            email=f"stranger-{uuid4().hex}@delete.test", name="Stranger", is_approved=True
        )
        owner = User(email=f"owner-{uuid4().hex}@delete.test", name="Owner", is_approved=True)
        db.add_all([stranger, owner])
        await db.flush()
        running = Job(owner_id=owner.id, research_question="Private research", status="running")
        expires_at = datetime.now(UTC) + timedelta(days=1)
        stranger_login, stranger_token = Session.issue(user_id=stranger.id, expires_at=expires_at)
        owner_login, owner_token = Session.issue(user_id=owner.id, expires_at=expires_at)
        db.add_all([running, stranger_login, owner_login])
        await db.commit()
    yield DeleteWorld(
        stranger_token=stranger_token, owner_token=owner_token, running_job_id=str(running.id)
    )
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == running.id))
        await db.execute(delete(User).where(User.id.in_([stranger.id, owner.id])))
        await db.commit()


async def _send_delete_event(client: Client, browser: SimulatedBrowser, job_id: str) -> None:
    """Emit ``delete-job`` for ``job_id`` from the page's own jobs table, as a
    forged client event would, and wait until its handler has finished."""
    table = next(
        element
        for element in client.elements.values()
        if isinstance(element, ui.table)
        and any(listener.type == "deleteJob" for listener in element._event_listeners.values())
    )
    before = set(background_tasks.running_tasks)
    UserInteraction(browser, {table}, None).trigger("delete-job", job_id)
    for task in background_tasks.running_tasks - before:
        await asyncio.wait_for(task, 10)


@pytest.mark.asyncio
async def test_a_forged_delete_event_opens_nothing_for_a_job_the_caller_does_not_own(
    delete_world: DeleteWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another user's running job and an id no job has answer alike, with no
    dialog, so the event cannot tell whether a job exists or is running."""
    monkeypatch.setattr(web_app._state, "job_manager", JobManager(jobs_dir=tmp_path))
    async with user_simulation(root=jobs_page) as browser:
        browser.http_client.cookies.set("session_token", delete_world.stranger_token)
        client = await browser.open("/")
        for job_id in (delete_world.running_job_id, str(uuid4())):
            await _send_delete_event(client, browser, job_id)
            await browser.should_not_see("Delete Job")
            await browser.should_not_see(ACTIVE_WARNING)


@pytest.mark.asyncio
async def test_the_owner_still_sees_the_active_job_warning(
    delete_world: DeleteWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's delete dialog opens and warns that the running job will be
    cancelled first."""
    monkeypatch.setattr(web_app._state, "job_manager", JobManager(jobs_dir=tmp_path))
    async with user_simulation(root=jobs_page) as browser:
        browser.http_client.cookies.set("session_token", delete_world.owner_token)
        client = await browser.open("/")
        await _send_delete_event(client, browser, delete_world.running_job_id)
        await browser.should_see("Delete Job")
        await browser.should_see(ACTIVE_WARNING)
