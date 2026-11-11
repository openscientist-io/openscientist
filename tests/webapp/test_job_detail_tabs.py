"""The Report and Chat tabs stay disabled until the job completes, with a
tooltip naming what waits, and the Research Log tab carries the live status."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, Session, User
from openscientist.job.types import JobInfo, JobStatus
from openscientist.job_manager import JobManager
from openscientist.webapp_components.components import common as components_common
from openscientist.webapp_components.pages.job_detail import (
    common,
    job_detail_page,
    report,
    timeline,
)
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage


@pytest.mark.parametrize("status", list(JobStatus))
def test_the_report_tab_is_enabled_exactly_for_completed_jobs(status: JobStatus) -> None:
    assert (common._report_tab_tooltip(status) is None) is (status is JobStatus.COMPLETED)


@pytest.mark.parametrize("status", list(JobStatus))
def test_the_chat_tab_is_enabled_exactly_for_completed_jobs(status: JobStatus) -> None:
    assert (common._chat_tab_tooltip(status) is None) is (status is JobStatus.COMPLETED)


@pytest.mark.parametrize("status", list(JobStatus))
def test_a_disabled_tab_names_what_waits(status: JobStatus) -> None:
    if status is JobStatus.COMPLETED:
        assert common._report_tab_tooltip(status) is None
    elif status is JobStatus.FAILED:
        assert common._report_tab_tooltip(status) == "The job failed, so it has no report."
    else:
        assert common._report_tab_tooltip(status) == (
            "The report is available once the job finishes."
        )
    if status is JobStatus.COMPLETED:
        assert common._chat_tab_tooltip(status) is None
    else:
        assert common._chat_tab_tooltip(status) == "Chat opens once the job finishes."


def _job(status: JobStatus) -> JobInfo:
    return JobInfo(
        job_id="job-1",
        research_question="Q",
        status=status,
        created_at="2026-01-01T00:00:00",
    )


def test_the_log_tab_carries_the_report_generation_status() -> None:
    job = _job(JobStatus.GENERATING_REPORT)
    assert timeline._status_line_for(job, None) == "Generating report..."
    assert timeline._status_line_for(job, {"agent_status": "leftover"}) == ("Generating report...")


def test_the_log_tab_carries_the_live_agent_status_while_running() -> None:
    job = _job(JobStatus.RUNNING)
    assert (
        timeline._status_line_for(job, {"agent_status": "Iteration 2: checking markers"})
        == "Iteration 2: checking markers"
    )


def test_the_log_tab_shows_no_line_without_a_live_status() -> None:
    job = _job(JobStatus.RUNNING)
    assert timeline._status_line_for(job, None) is None
    assert timeline._status_line_for(job, {"agent_status": None}) is None


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED]
)
def test_a_job_without_a_live_status_has_no_line(status: JobStatus) -> None:
    job = _job(status)
    assert timeline._status_line_for(job, {"agent_status": "leftover"}) is None


def test_the_report_tab_does_not_render_the_generation_status(tmp_path: Path) -> None:
    """While generating, the generation status lives in the log tab only."""
    context = SimpleNamespace(
        job_info=SimpleNamespace(status=JobStatus.GENERATING_REPORT),
        job_dir=tmp_path,
    )
    with (
        patch.object(report, "ui", MagicMock()),
        patch.object(report, "render_thinking_status", create=True) as thinking,
    ):
        report._render_report_tab(context, MagicMock(), MagicMock())  # type: ignore[arg-type]

    thinking.assert_not_called()


def _rendered_report_labels(status: JobStatus, job_dir: Path) -> list[str]:
    """The label texts the Report tab pane renders for a job of ``status``
    whose directory is ``job_dir``."""
    context = SimpleNamespace(job_info=SimpleNamespace(status=status), job_dir=job_dir)
    recorder = MagicMock()
    with patch.object(report, "ui", recorder):
        report._render_report_tab(context, MagicMock(), MagicMock())  # type: ignore[arg-type]
    return [c.args[0] for c in recorder.label.call_args_list if c.args]


def test_a_failed_job_rendered_report_tab_shows_no_report(tmp_path: Path) -> None:
    """A failed job keeps no report to show, so its pane carries only the
    waiting label and renders no report content."""
    labels = _rendered_report_labels(JobStatus.FAILED, tmp_path)
    assert labels == ["Report will be available when job completes"]


def test_a_completed_job_without_a_report_keeps_its_failure_label(tmp_path: Path) -> None:
    """A completed job whose report step wrote nothing keeps the red failure
    label, because its report never rendered."""
    labels = _rendered_report_labels(JobStatus.COMPLETED, tmp_path)
    assert labels == ["Report generation failed"]


def _rendered_labels(job: JobInfo, ks_data: dict[str, object] | None) -> list[str]:
    """The label texts the Research Log stats block renders for ``job``."""
    recorder = MagicMock()
    context = SimpleNamespace(
        job_info=job,
        ks_data=ks_data,
        owner_name=None,
        spend=0.0,
        direct_access=False,
        action_counts={},
    )
    with (
        patch.object(timeline, "ui", recorder),
        patch.object(components_common, "ui", recorder),
        patch.object(timeline, "is_client_connected", return_value=True),
    ):
        timeline._render_job_stats_content(context)  # type: ignore[arg-type]
    return [c.args[0] for c in recorder.label.call_args_list if c.args]


def test_the_stats_block_shows_the_report_generation_status_under_its_badges() -> None:
    job = JobInfo(
        job_id="job-1",
        research_question="Q",
        status=JobStatus.GENERATING_REPORT,
        created_at="2026-01-01T00:00:00",
    )
    labels = _rendered_labels(job, None)
    assert "Status: generating report" in labels
    assert "Generating report..." in labels


def test_a_finished_job_shows_no_live_status_line() -> None:
    job = JobInfo(
        job_id="job-1",
        research_question="Q",
        status=JobStatus.COMPLETED,
        created_at="2026-01-01T00:00:00",
    )
    labels = _rendered_labels(job, {"agent_status": "leftover"})
    assert "Status: completed" in labels
    assert "leftover" not in labels


@pytest.mark.requires_weasyprint
@pytest.mark.asyncio
async def test_a_tab_pane_spaces_its_sections_like_a_tab_panel(served_page) -> None:
    """The status line, the research question card and the timeline sit a gap
    apart, as they did inside Quasar's tab panel."""
    from nicegui import ui

    from openscientist.webapp_components.pages.job_detail import _tab_pane
    from tests.webapp.css_layout import box_by_id, layout

    panes: list[ui.element] = []

    async def build() -> None:
        with ui.tabs() as tabs:
            log = ui.tab("Research Log")
        tabs.set_value(log)
        panes.append(_tab_pane(tabs, log))

    await served_page("unused", build, lambda client: None)
    classes = " ".join(panes[0].classes)
    boxes = layout(
        f'<div class="{classes}"><div id="first" style="height:10px"></div>'
        '<div id="second" style="height:10px"></div></div>'
    )
    first, second = box_by_id(boxes, "first"), box_by_id(boxes, "second")
    assert second.position_y - (first.position_y + first.height) == pytest.approx(16)


class FailedReportJob(TypedDict):
    owner: User
    job_id: str
    token: str
    jobs_root: Path


@pytest_asyncio.fixture
async def failed_report_job(
    test_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[FailedReportJob]:
    """A failed job owned by an approved user with a live login, whose
    directory holds no final report, all committed."""
    jobs_root = tmp_path / "jobs"
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(
            email=f"failed-report-{uuid4().hex}@page.test",
            name="Failed Report Owner",
            is_approved=True,
        )
        db.add(owner)
        await db.flush()
        job = Job(owner_id=owner.id, research_question="Failed report", status="failed")
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add_all([job, login])
        await db.flush()
        (jobs_root / str(job.id)).mkdir(parents=True)
        await db.commit()
    yield {
        "owner": owner,
        "job_id": str(job.id),
        "token": str(login.id),
        "jobs_root": jobs_root,
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest.fixture
def failed_report_job_manager(
    failed_report_job: FailedReportJob, monkeypatch: pytest.MonkeyPatch
) -> JobManager:
    """Point the web's job manager at the test job directory."""
    manager = JobManager(jobs_dir=failed_report_job["jobs_root"])
    monkeypatch.setattr(web_app._state, "job_manager", manager)
    return manager


def _build_failed_report_page(failed_report_job: FailedReportJob):
    """The real page build for the failed job, with the signed-in user stored.

    The session-check deadline is its own concern, so the build runs without
    it.
    """
    owner = failed_report_job["owner"]
    job_id = failed_report_job["job_id"]

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


def _report_tab(client: Client) -> ui.tab:
    return next(
        element
        for element in client.elements.values()
        if isinstance(element, ui.tab) and element.props["label"] == "Report"
    )


@pytest.mark.asyncio
async def test_a_failed_job_greys_out_its_report_tab(
    database: Forwarder,
    failed_report_job: FailedReportJob,
    failed_report_job_manager: JobManager,
    served_page: ServedPage,
) -> None:
    """The failed job's Report tab is disabled with its own tooltip, and the
    page shows no report-failure label, because the job has no report at all."""
    client, report_tab = await served_page(
        failed_report_job["token"],
        _build_failed_report_page(failed_report_job),
        _report_tab,
    )
    assert report_tab.props["disable"] is True
    assert "os-tab-disabled" in report_tab.classes
    tooltips = [element for element in report_tab.descendants() if isinstance(element, ui.tooltip)]
    assert [tooltip.text for tooltip in tooltips] == ["The job failed, so it has no report."]
    labels = [
        str(element.text) for element in client.elements.values() if isinstance(element, ui.label)
    ]
    assert "Report generation failed" not in labels
