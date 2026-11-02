"""The Report and Chat tabs grey out until the job ends, with a tooltip
naming what waits, and the Research Log tab carries the live status."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import common, report, timeline

_ENDED = (JobStatus.COMPLETED, JobStatus.FAILED)


@pytest.mark.parametrize("status", list(JobStatus))
def test_the_report_tab_is_enabled_exactly_for_finished_jobs(status: JobStatus) -> None:
    assert (common._report_tab_tooltip(status) is None) is (status in _ENDED)


@pytest.mark.parametrize("status", list(JobStatus))
def test_the_chat_tab_is_enabled_exactly_for_completed_jobs(status: JobStatus) -> None:
    assert (common._chat_tab_tooltip(status) is None) is (status is JobStatus.COMPLETED)


@pytest.mark.parametrize("status", list(JobStatus))
def test_a_disabled_tab_names_what_waits(status: JobStatus) -> None:
    if status in _ENDED:
        assert common._report_tab_tooltip(status) is None
    else:
        assert common._report_tab_tooltip(status) == (
            "The report is available once the job finishes."
        )
    if status is JobStatus.COMPLETED:
        assert common._chat_tab_tooltip(status) is None
    else:
        assert common._chat_tab_tooltip(status) == "Chat opens once the job finishes."


def test_the_log_tab_carries_the_report_generation_status() -> None:
    job = SimpleNamespace(status=JobStatus.GENERATING_REPORT)
    assert timeline._status_line_for(job, None) == "Generating report..."
    assert timeline._status_line_for(job, {"agent_status": "leftover"}) == "Generating report..."


def test_the_log_tab_carries_the_live_agent_status_while_running() -> None:
    job = SimpleNamespace(status=JobStatus.RUNNING)
    assert timeline._status_line_for(job, {"agent_status": "Iteration 2: checking markers"}) == (
        "Iteration 2: checking markers"
    )


def test_the_log_tab_shows_no_line_without_a_live_status() -> None:
    job = SimpleNamespace(status=JobStatus.RUNNING)
    assert timeline._status_line_for(job, None) is None
    assert timeline._status_line_for(job, {"agent_status": None}) is None


@pytest.mark.parametrize(
    "status", [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.QUEUED]
)
def test_a_job_without_a_live_status_has_no_line(status: JobStatus) -> None:
    job = SimpleNamespace(status=status)
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
        report._render_report_tab(context)  # type: ignore[arg-type]

    thinking.assert_not_called()
