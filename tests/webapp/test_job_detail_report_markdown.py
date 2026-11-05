"""The job page converts a large final report off the event loop.

The Report tab renders ``final_report.md`` with NiceGUI's markdown element,
and the markdown-to-HTML conversion is the heavy part. These tests build the
real page with a 2 MiB report and check that the conversion does not stall
the loop (a heartbeat task records the gaps between its beats) and that the
first HTML still carries the rendered report.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, Session, User
from openscientist.job_manager import JobManager
from openscientist.webapp_components.pages.job_detail import job_detail_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage

_REPORT_BYTES = 2 * 1024 * 1024
_PMID = "12345678"
# The heartbeat sleeps 10 ms per beat, so a gap past this means the loop
# stayed busy past two beats while the page built.
_MAX_LOOP_GAP = 0.100
_PARAGRAPH = (
    "The corrected assay recovered the target in every replicate, and the "
    "residual variance stayed below the noise floor across all conditions. "
)


class ReportJob(TypedDict):
    owner: User
    job_id: str
    token: str
    marker: str
    jobs_root: Path


def _report_markdown(marker: str) -> str:
    """A final report of at least 2 MiB with a marker heading, PMID links,
    a code fence and a table."""
    report = (
        f"# Final report {marker}\n\n"
        f"## Method\n\n{_PARAGRAPH * 80}\n"
        f"The assay is described in PMID: {_PMID}; the follow-up carries PMID {_PMID}.\n\n"
        "```python\nprint('reproducible')\n```\n\n"
        "| step | outcome |\n|---|---|\n| one | pass |\n| two | pass |\n"
    )
    note = f"## Note\n\n{_PARAGRAPH * 80}\n"
    while len((report + note).encode()) < _REPORT_BYTES:
        report += note
    return report


@pytest_asyncio.fixture
async def report_job(test_engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[ReportJob]:
    """A completed job owned by an approved user with a live login, whose
    directory holds a 2 MiB final report, all committed."""
    jobs_root = tmp_path / "jobs"
    marker = uuid4().hex
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"report-{uuid4().hex}@page.test", name="Report Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(owner_id=owner.id, research_question="Report render", status="completed")
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add_all([job, login])
        await db.flush()
        job_dir = jobs_root / str(job.id)
        job_dir.mkdir(parents=True)
        (job_dir / "final_report.md").write_text(_report_markdown(marker), encoding="utf-8")
        await db.commit()
    yield {
        "owner": owner,
        "job_id": str(job.id),
        "token": str(login.id),
        "marker": marker,
        "jobs_root": jobs_root,
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest.fixture
def report_job_manager(report_job: ReportJob, monkeypatch: pytest.MonkeyPatch) -> JobManager:
    """Point the web's job manager at the test job directory."""
    manager = JobManager(jobs_dir=report_job["jobs_root"])
    monkeypatch.setattr(web_app._state, "job_manager", manager)
    return manager


def _build_report_page(report_job: ReportJob):
    """The real page build for the report job, with the signed-in user stored.

    The session-check deadline is its own concern, so the build runs without
    it, and a heavy report never reads as a database outage while the loop
    is measured.
    """
    owner = report_job["owner"]
    job_id = report_job["job_id"]

    async def build() -> None:
        app.storage.user.update(
            {
                "user_id": str(owner.id),
                "email": owner.email,
                "name": owner.name,
                "is_admin": False,
                "is_approved": True,
                "can_start_jobs": True,
                "authenticated": True,
            }
        )
        await job_detail_page.__wrapped__(job_id)  # type: ignore[attr-defined]

    return build


async def _max_loop_gap(stop: asyncio.Event) -> float:
    """Beat every 10 ms until ``stop`` is set, returning the largest gap
    between two beats, which is how long the loop stayed busy."""
    last = time.monotonic()
    max_gap = 0.0
    while not stop.is_set():
        await asyncio.sleep(0.01)
        now = time.monotonic()
        max_gap = max(max_gap, now - last)
        last = now
    return max_gap


def _sent_report_html(client: Client, marker: str) -> str:
    """The HTML of the report's markdown element at the moment the page went out."""
    report = next(
        element
        for element in client.elements.values()
        if isinstance(element, ui.markdown) and marker in element.content
    )
    return str(report._props["innerHTML"])


@pytest.mark.asyncio
async def test_building_a_page_with_a_two_mib_report_does_not_stall_the_loop(
    database: Forwarder,
    report_job: ReportJob,
    report_job_manager: JobManager,
    served_page: ServedPage,
) -> None:
    """The 2 MiB report converts off the loop, so the heartbeat keeps beating
    every 10 ms while the page builds."""
    stop = asyncio.Event()
    beater = asyncio.create_task(_max_loop_gap(stop))
    try:
        await served_page(report_job["token"], _build_report_page(report_job), lambda client: None)
    finally:
        stop.set()
    gap = await beater
    assert gap < _MAX_LOOP_GAP, f"the loop stayed busy for {gap * 1000:.0f} ms"


@pytest.mark.asyncio
async def test_the_first_html_carries_the_rendered_report(
    database: Forwarder,
    report_job: ReportJob,
    report_job_manager: JobManager,
    served_page: ServedPage,
) -> None:
    """The report renders during the build, and the sent HTML keeps its
    heading, PMID links, code blocks and tables."""
    marker = report_job["marker"]
    _, html = await served_page(
        report_job["token"],
        _build_report_page(report_job),
        lambda client: _sent_report_html(client, marker),
    )
    assert f"<h1>Final report {marker}</h1>" in html
    assert f"https://pubmed.ncbi.nlm.nih.gov/{_PMID}/" in html
    assert '<div class="codehilite">' in html
    assert "<table>" in html
