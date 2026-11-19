"""Deleting a job from its dialog confirms the deletion even when the page
is redrawn while the job is being deleted."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, ui
from nicegui.client import Client
from nicegui.testing import user_simulation
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth.middleware import require_auth
from openscientist.database.models import Job, Session, User
from openscientist.job.types import JobInfo, JobStatus
from openscientist.job_manager import JobManager
from openscientist.webapp_components.components.dialogs import render_delete_dialog
from tests.db_outage import Forwarder


@pytest_asyncio.fixture
async def owned_running_job(
    database: Forwarder, test_engine: AsyncEngine
) -> AsyncIterator[tuple[str, UUID]]:
    """A signed-in owner's session token and the id of their running job."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@delete.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        login, token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        job = Job(owner_id=owner.id, research_question="Delete me?", status="running")
        db.add_all([login, job])
        await db.commit()
    yield token, job.id
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


class _RedrawnWhileDeleting:
    """The job manager at its boundary. Deleting removes the job row, and the
    job page's status poll, finding the job gone, clears the page before the
    deletion returns."""

    ACTIVE_STATUSES = JobManager.ACTIVE_STATUSES

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.client: Client | None = None
        self.cancelled: list[str] = []

    async def get_job(self, job_id: str) -> JobInfo:
        return JobInfo(
            job_id=job_id,
            research_question="Delete me?",
            status=JobStatus.RUNNING,
            created_at="2026-11-17T18:19:00",
        )

    async def cancel_job(self, job_id: str) -> None:
        self.cancelled.append(job_id)

    async def delete_job(self, job_id: str) -> None:
        async with AsyncSession(self.engine) as db:
            await db.execute(delete(Job).where(Job.id == UUID(job_id)))
            await db.commit()
        assert self.client is not None
        self.client.content.clear()


@pytest.mark.asyncio
async def test_deleting_a_job_whose_page_is_redrawn_confirms_the_deletion(
    owned_running_job: tuple[str, UUID],
    test_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token, job_id = owned_running_job
    manager = _RedrawnWhileDeleting(test_engine)
    navigated: list[str] = []

    async def page() -> None:
        manager.client = ui.context.client
        dialog = render_delete_dialog(
            str(job_id), cast("JobManager", manager), on_deleted=lambda: navigated.append("/jobs")
        )
        ui.button("Remove job", on_click=dialog.open)

    caplog.set_level(logging.ERROR)
    async with user_simulation(root=require_auth(page)) as browser:
        browser.http_client.cookies.set("session_token", token)
        await browser.open("/")
        browser.find("Remove job").click()
        await browser.should_see("Delete Job")
        browser.find("Delete").click()
        await browser.should_see(f"Job {str(job_id)[-8:]} deleted successfully")

    assert manager.cancelled == [str(job_id)]
    assert navigated == ["/jobs"]
    assert "Failed to delete job" not in caplog.text


class _FailsAfterRedraw(_RedrawnWhileDeleting):
    """A deletion that raises after the page was redrawn."""

    def __init__(self, engine: AsyncEngine, failure: Exception) -> None:
        super().__init__(engine)
        self.failure = failure

    async def delete_job(self, job_id: str) -> None:
        assert self.client is not None
        self.client.content.clear()
        raise self.failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "message", "logged"),
    [
        (
            ValueError("Cannot delete running job"),
            "Invalid job or job cannot be deleted in its current state.",
            False,
        ),
        (RuntimeError("database gone"), "Failed to delete job. Please try again.", True),
    ],
)
async def test_a_deletion_that_raises_reports_the_failure_and_stays_on_the_page(
    owned_running_job: tuple[str, UUID],
    test_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    message: str,
    logged: bool,
) -> None:
    token, job_id = owned_running_job
    manager = _FailsAfterRedraw(test_engine, failure)
    navigated: list[str] = []

    async def page() -> None:
        manager.client = ui.context.client
        dialog = render_delete_dialog(
            str(job_id), cast("JobManager", manager), on_deleted=lambda: navigated.append("/jobs")
        )
        ui.button("Remove job", on_click=dialog.open)

    caplog.set_level(logging.ERROR)
    async with user_simulation(root=require_auth(page)) as browser:
        browser.http_client.cookies.set("session_token", token)
        await browser.open("/")
        browser.find("Remove job").click()
        await browser.should_see("Delete Job")
        before = set(background_tasks.running_tasks)
        browser.find("Delete").click()
        for task in background_tasks.running_tasks - before:
            await asyncio.wait_for(task, 10)
        await browser.should_see(message)
        await browser.should_not_see("deleted successfully")

    assert navigated == []
    assert ("Failed to delete job" in caplog.text) is logged
