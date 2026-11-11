"""The Report tab converts the markdown report when it is first opened.

The markdown-to-HTML conversion is the heavy part of the report, and it runs
when the Report tab is activated, never during the page build, so a large
report cannot push the build past NiceGUI's response deadline. Most tests
stub the conversion with a slow one and check that the build stays fast and
conversion-free, that the first HTML carries only the loading state, that
the first activation renders the report without stalling the event loop and
a second one does not convert again, and that a client that disconnects
during the conversion raises nothing. One test runs the real conversion and
checks what it renders.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, TypedDict
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app, background_tasks, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, Session, User
from openscientist.job_manager import JobManager
from openscientist.webapp_components.pages.job_detail import job_detail_page, report
from tests.db_outage import Forwarder
from tests.helpers import bounded_peak
from tests.webapp.conftest import ServedPage

# NiceGUI's deadline for the page build, in seconds.
_RESPONSE_TIMEOUT = 3.0
# The stub conversion's delay, past the deadline so a conversion that ran
# during the build cannot hide in the build-time assertion.
_CONVERSION_DELAY = 3.5
# The heartbeat sleeps 10 ms per beat, so a gap past this means the loop
# stayed busy past two beats while the report converted.
_MAX_LOOP_GAP = 0.100
_PMID = "12345678"
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
    """A final report with a marker heading, PMID links, a code fence and a
    table, at the size the real conversion turns around in milliseconds."""
    return (
        f"# Final report {marker}\n\n"
        f"## Method\n\n{_PARAGRAPH * 80}\n"
        f"The assay is described in PMID: {_PMID}; the follow-up carries PMID {_PMID}.\n\n"
        "```python\nprint('reproducible')\n```\n\n"
        "| step | outcome |\n|---|---|\n| one | pass |\n| two | pass |\n"
        f"## Note\n\n{_PARAGRAPH * 80}\n"
    )


@pytest_asyncio.fixture
async def report_job(test_engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[ReportJob]:
    """A completed job owned by an approved user with a live login, whose
    directory holds a final report, all committed."""
    jobs_root = tmp_path / "jobs"
    marker = uuid4().hex
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"report-{uuid4().hex}@page.test", name="Report Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(owner_id=owner.id, research_question="Report render", status="completed")
        login, login_token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add_all([job, login])
        await db.flush()
        job_dir = jobs_root / str(job.id)
        job_dir.mkdir(parents=True)
        (job_dir / "final_report.md").write_text(_report_markdown(marker), encoding="utf-8")
        await db.commit()
    yield {
        "owner": owner,
        "job_id": str(job.id),
        "token": login_token,
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
    it.
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


def _slow_conversion(marker: str, delay: float) -> tuple[Callable[[Path], str | None], list[str]]:
    """A stand-in for the report conversion that takes ``delay`` seconds,
    plus the log of the files it converted."""
    calls: list[str] = []

    def convert(report_path: Path) -> str | None:
        calls.append(report_path.name)
        time.sleep(delay)
        return f"# Final report {marker}\n\nConverted on tab activation."

    return convert, calls


def _markdown_elements(client: Client) -> list[ui.markdown]:
    return [element for element in client.elements.values() if isinstance(element, ui.markdown)]


def _placeholder_present(client: Client) -> bool:
    return any(
        "Converting the report" in str(element.text)
        for element in client.elements.values()
        if isinstance(element, ui.label)
    )


def _too_large_label_present(client: Client) -> bool:
    return any(
        "The report is too large to show." in str(element.text)
        for element in client.elements.values()
        if isinstance(element, ui.label)
    )


def _tabs(client: Client) -> ui.tabs:
    return next(element for element in client.elements.values() if isinstance(element, ui.tabs))


def _tab_names(client: Client) -> dict[str, str]:
    return {
        tab.props["label"]: tab.props["name"]
        for tab in client.elements.values()
        if isinstance(tab, ui.tab)
    }


async def _rendered_report(client: Client, marker: str) -> ui.markdown:
    """The rendered report element, once the activation's conversion has
    landed."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rendered = [element for element in _markdown_elements(client) if marker in element.content]
        if rendered:
            return rendered[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"the report {marker!r} did not render within 5 s")


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


@pytest.mark.asyncio
async def test_the_build_sends_fast_and_never_converts(
    database: Forwarder,
    report_job: ReportJob,
    report_job_manager: JobManager,
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversion slower than the response deadline stays out of the build,
    so the page sends fast and the first HTML carries the loading state."""
    convert, calls = _slow_conversion(report_job["marker"], _CONVERSION_DELAY)
    monkeypatch.setattr(report, "_transformed_report_markdown", convert)
    start = time.monotonic()
    client, sent = await served_page(
        report_job["token"],
        _build_report_page(report_job),
        lambda client: (
            _markdown_elements(client),
            _placeholder_present(client),
        ),
    )
    build_seconds = time.monotonic() - start
    markdowns, placeholder = sent
    assert calls == [], "the conversion ran during the page build"
    assert markdowns == [], "the first HTML carries the rendered report"
    assert placeholder, "the first HTML lacks the report's loading state"
    assert build_seconds < _RESPONSE_TIMEOUT - 0.5, (
        f"the page build took {build_seconds:.2f} s, close to the response deadline"
    )


@pytest.mark.asyncio
async def test_activating_the_report_tab_renders_it_exactly_once(
    database: Forwarder,
    report_job: ReportJob,
    report_job_manager: JobManager,
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first activation converts and renders the report while the event
    loop keeps beating, and a second activation reuses the rendered report
    without converting again."""
    marker = report_job["marker"]
    convert, calls = _slow_conversion(marker, _CONVERSION_DELAY)
    monkeypatch.setattr(report, "_transformed_report_markdown", convert)
    client, _ = await served_page(
        report_job["token"],
        _build_report_page(report_job),
        lambda client: _markdown_elements(client),
    )
    assert calls == []
    names = _tab_names(client)
    tabs = _tabs(client)

    stop = asyncio.Event()
    beater = asyncio.create_task(_max_loop_gap(stop))
    try:
        tabs.set_value(names["Report"])
        rendered = await _rendered_report(client, marker)
    finally:
        stop.set()
    assert calls == ["final_report.md"]
    assert f"<h1>Final report {marker}</h1>" in rendered._props["innerHTML"]
    assert not _placeholder_present(client)
    gap = await beater
    assert gap < _MAX_LOOP_GAP, f"the loop stayed busy for {gap * 1000:.0f} ms"

    tabs.set_value(names["Research Log"])
    tabs.set_value(names["Report"])
    await asyncio.sleep(0.2)
    assert calls == ["final_report.md"], "the second activation converted again"


@pytest.mark.asyncio
async def test_the_real_conversion_renders_links_code_and_tables(
    database: Forwarder,
    report_job: ReportJob,
    report_job_manager: JobManager,
    served_page: ServedPage,
) -> None:
    """Without a stub, the real conversion lands in the tab and renders the
    marker heading, PMID links, code blocks and tables."""
    marker = report_job["marker"]
    client, _ = await served_page(
        report_job["token"],
        _build_report_page(report_job),
        lambda client: _markdown_elements(client),
    )
    _tabs(client).set_value(_tab_names(client)["Report"])
    rendered = await _rendered_report(client, marker)
    html = rendered._props["innerHTML"]
    assert f"<h1>Final report {marker}</h1>" in html
    assert f"https://pubmed.ncbi.nlm.nih.gov/{_PMID}/" in html
    assert '<div class="codehilite">' in html
    assert "<table>" in html


@pytest.mark.asyncio
async def test_a_disconnect_during_the_conversion_raises_nothing(
    database: Forwarder,
    report_job: ReportJob,
    report_job_manager: JobManager,
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that disconnects while the conversion runs raises nothing and
    gets no render after the disconnect."""
    marker = report_job["marker"]
    convert, calls = _slow_conversion(marker, 1.0)
    monkeypatch.setattr(report, "_transformed_report_markdown", convert)
    client, _ = await served_page(
        report_job["token"],
        _build_report_page(report_job),
        lambda client: None,
    )
    _tabs(client).set_value(_tab_names(client)["Report"])
    activation = next(
        task for task in background_tasks.running_tasks if "_activate_report_tab" in task.get_name()
    )
    await asyncio.sleep(0.1)  # let the conversion start in its thread
    client.delete()
    await activation  # must not raise
    assert calls == ["final_report.md"]
    assert _markdown_elements(client) == []


@pytest.mark.asyncio
async def test_an_oversized_report_is_shown_as_too_large(
    database: Forwarder,
    report_job: ReportJob,
    report_job_manager: JobManager,
    served_page: ServedPage,
) -> None:
    """The agent plants a sparse report well past the size cap, so the tab's
    read is refused and says the report is too large, without ever reading it."""
    from openscientist.artifact_packager import MAX_ARTIFACT_FILE_SIZE_BYTES

    job_dir = report_job["jobs_root"] / report_job["job_id"]
    os.truncate(job_dir / "final_report.md", MAX_ARTIFACT_FILE_SIZE_BYTES * 4)
    client, _ = await served_page(
        report_job["token"],
        _build_report_page(report_job),
        lambda client: None,
    )
    _tabs(client).set_value(_tab_names(client)["Report"])
    with bounded_peak():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _too_large_label_present(client):
            await asyncio.sleep(0.01)
    assert _too_large_label_present(client), "the too large message did not land within 5 s"
    assert _markdown_elements(client) == []


def _frames(client: Client) -> list[ui.element]:
    return [element for element in client.elements.values() if element.tag == "iframe"]


@pytest.mark.asyncio
async def test_a_saved_html_report_loads_in_its_frame_on_the_first_visit_only(
    database: Forwarder,
    report_job: ReportJob,
    report_job_manager: JobManager,
    served_page: ServedPage,
) -> None:
    """A job that saved its HTML report frames it instead of the markdown
    view. The frame fetches nothing until the Report tab first opens, so a
    visit to the log does not render the report, and later visits keep the
    loaded frame instead of fetching the report again."""
    job_dir = report_job["jobs_root"] / report_job["job_id"]
    (job_dir / "final_report.html").write_text("<html><body>Saved</body></html>", "utf-8")
    client, (frames, placeholder) = await served_page(
        report_job["token"],
        _build_report_page(report_job),
        lambda client: (_frames(client), _placeholder_present(client)),
    )
    assert len(frames) == 1
    assert not placeholder
    [frame] = frames
    assert "src" not in frame.props, "the frame fetched the report before the tab opened"

    names = _tab_names(client)
    tabs = _tabs(client)
    tabs.set_value(names["Report"])
    src = frame.props.get("src")
    assert src == f"/web/jobs/{report_job['job_id']}/report.html"

    tabs.set_value(names["Chat"])
    tabs.set_value(names["Report"])
    assert _frames(client) == [frame]
    assert frame.props["src"] == src
