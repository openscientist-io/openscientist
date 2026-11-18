"""The share dialog reports the outcome of every action, even when the page
deletes the control that started it while the action is still running."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from nicegui.testing import user_simulation
from nicegui.testing.user_interaction import UserInteraction
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth.middleware import require_auth
from openscientist.database.models import Job, JobShare, Session, User
from openscientist.webapp_components.components import dialogs
from openscientist.webapp_components.utils.permissions import JobAccess
from tests.db_outage import Forwarder


@dataclass(frozen=True)
class SharedJob:
    """A signed-in owner's job shared with two readers."""

    token: str
    job_id: str


@pytest_asyncio.fixture
async def shared_job(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[SharedJob]:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@revoke.test", name="Owner", is_approved=True)
        readers = [
            User(email=f"reader{n}-{uuid4().hex}@revoke.test", name=f"Reader {n}") for n in (1, 2)
        ]
        db.add_all([owner, *readers])
        await db.flush()
        job = Job(owner_id=owner.id, research_question="Shared twice", status="completed")
        login, token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add_all([job, login])
        await db.flush()
        db.add_all(
            JobShare(job_id=job.id, shared_with_user_id=reader.id, permission_level="view")
            for reader in readers
        )
        await db.commit()
    yield SharedJob(token=token, job_id=str(job.id))
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, *(r.id for r in readers)])))
        await db.commit()


def _redraw_during_access_check(monkeypatch: pytest.MonkeyPatch, page: list[Client]) -> None:
    """Clear the page while the dialog checks the caller's access to the job,
    the way the job page's poll does when it redraws the page. The dialog's
    elements reference each other, so they die at the next garbage
    collection, which a live server runs at any moment."""
    real_access = dialogs.current_job_access

    async def redrawn_access(job_id: str, expected_user_id: str) -> JobAccess | None:
        page[0].content.clear()
        gc.collect()
        return await real_access(job_id, expected_user_id)

    monkeypatch.setattr(dialogs, "current_job_access", redrawn_access)


@pytest.mark.asyncio
async def test_making_a_job_public_is_confirmed_although_the_page_redraws(
    shared_job: SharedJob, test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    page_client: list[Client] = []
    _redraw_during_access_check(monkeypatch, page_client)

    async def page() -> None:
        page_client.append(ui.context.client)
        dialog = dialogs.render_share_dialog(shared_job.job_id)
        ui.button("Open share dialog", on_click=dialog.open)

    async with user_simulation(root=require_auth(page)) as browser:
        browser.http_client.cookies.set("session_token", shared_job.token)
        await browser.open("/")
        browser.find("Open share dialog").click()
        await browser.should_see("Current Shares", retries=50)
        browser.find(kind=ui.switch).click()
        await browser.should_see(
            "Any signed-in user with the link can now view this job", retries=50
        )

    async with AsyncSession(test_engine) as db:
        job = await db.get(Job, UUID(shared_job.job_id))
        assert job is not None and job.is_public


@pytest.mark.asyncio
async def test_two_revokes_in_a_row_both_revoke_and_confirm(
    shared_job: SharedJob, test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    released = [asyncio.Event(), asyncio.Event()]
    entered: list[int] = []
    real_access = dialogs.current_job_access

    async def held_access(job_id: str, expected_user_id: str) -> JobAccess | None:
        turn = len(entered)
        entered.append(turn)
        await released[turn].wait()
        return await real_access(job_id, expected_user_id)

    monkeypatch.setattr(dialogs, "current_job_access", held_access)

    async def page() -> None:
        dialog = dialogs.render_share_dialog(shared_job.job_id)
        ui.button("Open share dialog", on_click=dialog.open)

    async with user_simulation(root=require_auth(page)) as browser:
        browser.http_client.cookies.set("session_token", shared_job.token)
        await browser.open("/")
        browser.find("Open share dialog").click()
        await browser.should_see("Current Shares", retries=50)
        revoke_buttons = [
            button
            for button in browser.find(kind=ui.button).elements
            if isinstance(button, ui.button) and button.props.get("icon") == "delete"
        ]
        assert len(revoke_buttons) == 2
        for button in revoke_buttons:
            UserInteraction(browser, {button}, None).click()
        for _ in range(100):
            if len(entered) == 2:
                break
            await asyncio.sleep(0.05)
        assert entered == [0, 1]

        released[0].set()
        await browser.should_see("Share revoked successfully", retries=50)
        released[1].set()
        for _ in range(100):
            if browser.notify.messages.count("Share revoked successfully") == 2:
                break
            await asyncio.sleep(0.05)
        assert browser.notify.messages == ["Share revoked successfully"] * 2
        await browser.should_see("No shares yet", retries=50)

    async with AsyncSession(test_engine) as db:
        remaining = await db.scalars(select(JobShare).where(JobShare.job_id == shared_job.job_id))
        assert list(remaining) == []
