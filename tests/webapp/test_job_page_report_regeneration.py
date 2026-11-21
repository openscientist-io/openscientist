"""The job page offers an admin the report regeneration of a job whose report
step failed, and shows an admin why the last regeneration of a completed job
failed."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Administrator, Job, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage

PageFor = Callable[..., Awaitable[Client]]


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=JobStatus.FAILED)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )


@pytest_asyncio.fixture
async def page_for(
    database: Forwarder, test_engine: AsyncEngine, served_page: ServedPage
) -> AsyncIterator[PageFor]:
    """Builds the page of a job in the given state, owned by an admin or by a
    signed-in user who is not one."""
    users: list[UUID] = []

    async def build(
        *, admin: bool, status: str, failed_phase: str | None, error: str | None
    ) -> Client:
        async with AsyncSession(test_engine, expire_on_commit=False) as db:
            owner = User(email=f"owner-{uuid4().hex}@regen.test", name="Owner", is_approved=True)
            db.add(owner)
            await db.flush()
            users.append(owner.id)
            if admin:
                db.add(Administrator(user_id=owner.id))
            job = Job(
                owner_id=owner.id,
                research_question="Does the report come back?",
                status=status,
                failed_phase=failed_phase,
                error_message=error,
            )
            login, token = Session.issue(
                user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
            )
            db.add_all([job, login])
            await db.commit()
        client, _ = await served_page(token, lambda: job_detail_page(str(job.id)), lambda c: None)
        return client

    yield build
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.owner_id.in_(users)))
        await db.execute(delete(Administrator).where(Administrator.user_id.in_(users)))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


def _texts(client: Client) -> list[str]:
    with client:
        return [
            e.text
            for e in client.elements.values()
            if isinstance(e, ui.label | ui.button) and isinstance(e.text, str)
        ]


_REPORT_FAILURE = "Report generation failed: Model stopped streaming"


@pytest.mark.asyncio
async def test_an_admin_can_regenerate_a_report_that_failed(page_for: PageFor) -> None:
    client = await page_for(
        admin=True, status="failed", failed_phase="report", error=_REPORT_FAILURE
    )
    assert "Regenerate Report" in _texts(client)


@pytest.mark.asyncio
@pytest.mark.parametrize(("admin", "failed_phase"), [(False, "report"), (True, "iterations")])
async def test_regeneration_is_not_offered_without_admin_rights_or_findings(
    page_for: PageFor, admin: bool, failed_phase: str
) -> None:
    client = await page_for(
        admin=admin, status="failed", failed_phase=failed_phase, error=_REPORT_FAILURE
    )
    assert "Regenerate Report" not in _texts(client)


@pytest.mark.asyncio
async def test_an_admin_sees_why_the_last_regeneration_of_a_completed_job_failed(
    page_for: PageFor,
) -> None:
    client = await page_for(
        admin=True,
        status="completed",
        failed_phase=None,
        error="Agent container exited with code 1",
    )
    texts = _texts(client)
    assert "The last report regeneration failed" in texts
    assert "Agent container exited with code 1" in texts


@pytest.mark.asyncio
async def test_an_owner_who_is_not_an_admin_is_not_shown_a_regeneration_failure(
    page_for: PageFor,
) -> None:
    client = await page_for(
        admin=False,
        status="completed",
        failed_phase=None,
        error="Agent container exited with code 1",
    )
    texts = _texts(client)
    assert "The last report regeneration failed" not in texts
    assert "Agent container exited with code 1" not in texts
