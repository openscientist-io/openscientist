"""The job page names the environment a run recorded: the OpenScientist build,
the agent harness and the container."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from openscientist.database.models import Job
from openscientist.job.types import JobInfo, JobStatus
from openscientist.webapp_components.pages import job_detail

_RECORDED = {
    "openscientist_commit": "abc123def456",
    "openscientist_build_time": "2026-10-30T12:00:00Z",
    "docker_container_id": "0f1e2d3c4b5a",
    "agent_harness": "claude_code",
    "agent_harness_version": "2.1.3",
    "claude_code_version": "2.1.3",
    "claude_code_sdk_version": "0.9.1",
}


def test_the_run_environment_names_the_build_the_harness_and_the_container() -> None:
    text = job_detail._run_environment_text(_RECORDED)
    assert text is not None
    for part in (
        "abc123def456",
        "2026-10-30T12:00:00Z",
        "Claude Code 2.1.3",
        "0.9.1",
        "0f1e2d3c4b5a",
    ):
        assert part in text


def test_a_codex_run_names_its_harness() -> None:
    text = job_detail._run_environment_text(
        {"agent_harness": "codex", "agent_harness_version": "0.163.0"}
    )
    assert text is not None
    assert "Codex 0.163.0" in text


def test_a_run_with_no_recorded_environment_shows_none() -> None:
    assert job_detail._run_environment_text(None) is None
    assert job_detail._run_environment_text({}) is None


def test_job_info_carries_the_recorded_environment() -> None:
    now = datetime.now(UTC)
    job = Job(research_question="Q?", status="completed", max_iterations=3, version_info=_RECORDED)
    job.created_at = now
    job.updated_at = now
    assert JobInfo.from_db_model(job).version_info == _RECORDED


def test_the_job_page_shows_the_run_environment() -> None:
    info = JobInfo(
        job_id="j1",
        research_question="Q?",
        status=JobStatus.COMPLETED,
        created_at="2026-10-30T12:00:00Z",
        version_info=_RECORDED,
    )
    context = SimpleNamespace(job_info=info, ks_data=None)
    with (
        patch.object(job_detail, "ui") as mock_ui,
        patch.object(job_detail, "is_client_connected", return_value=True),
        patch.object(job_detail, "render_stat_badges"),
    ):
        job_detail._render_job_stats_content(context)  # type: ignore[arg-type]
    labels = [str(call.args[0]) for call in mock_ui.label.call_args_list if call.args]
    assert any("abc123def456" in label for label in labels)
