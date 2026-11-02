"""A report written at the end of a run names the environment the run recorded."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Job
from openscientist.orchestrator.discovery import _try_generate_report_pdf

_RECORDED = {
    "openscientist_commit": "abc123def456",
    "agent_harness": "claude_code",
    "agent_harness_version": "2.1.3",
}


@pytest.mark.asyncio
async def test_a_generated_report_names_the_run_environment(
    test_engine: AsyncEngine, tmp_path: Path
) -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        job = Job(research_question="Q?", status="generating_report", version_info=_RECORDED)
        session.add(job)
        await session.commit()
    try:
        job_dir = tmp_path / str(job.id)
        job_dir.mkdir()
        report_path = job_dir / "final_report.md"
        report_path.write_text("# Report\n\nFindings.\n", encoding="utf-8")

        await _try_generate_report_pdf(report_path)

        html = (job_dir / "final_report.html").read_text(encoding="utf-8")
        assert "OpenScientist abc123def456" in html
        assert "Claude Code 2.1.3" in html
    finally:
        async with AsyncSession(test_engine) as session:
            await session.execute(delete(Job).where(Job.id == job.id))
            await session.commit()


@pytest.mark.asyncio
async def test_a_report_for_an_unknown_job_still_renders(tmp_path: Path) -> None:
    """The footer is best effort, so a missing record never costs the report."""
    job_dir = tmp_path / "not-a-job"
    job_dir.mkdir()
    report_path = job_dir / "final_report.md"
    report_path.write_text("# Report\n", encoding="utf-8")
    await _try_generate_report_pdf(report_path)
    assert "Run environment" not in (job_dir / "final_report.html").read_text(encoding="utf-8")
