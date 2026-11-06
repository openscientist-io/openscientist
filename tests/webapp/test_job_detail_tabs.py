"""The Report and Chat tabs grey out until the job ends, with a tooltip
naming what waits, and the Research Log tab carries the live status."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openscientist.job.types import JobInfo, JobStatus
from openscientist.webapp_components.components import common as components_common
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
        report._render_report_tab(context, MagicMock(), MagicMock())  # type: ignore[arg-type]

    thinking.assert_not_called()


def _rendered_labels(job: JobInfo, ks_data: dict[str, object] | None) -> list[str]:
    """The label texts the Research Log stats block renders for ``job``."""
    recorder = MagicMock()
    context = SimpleNamespace(job_info=job, ks_data=ks_data, owner_name=None)
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
