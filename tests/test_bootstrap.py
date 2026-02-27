"""Tests for legacy filesystem bootstrap into the database."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.bootstrap import bootstrap_jobs_from_filesystem
from shandy.database.models import (
    AnalysisLog,
    FeedbackHistory,
    Finding,
    Hypothesis,
    IterationSummary,
    Job,
    JobDataFile,
    Literature,
    Plot,
)
from tests.helpers import fake_admin_session


def _write_json(path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


@pytest.mark.asyncio
async def test_bootstrap_creates_job_and_syncs_modern_knowledge_state(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "shandy.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    job_id = str(uuid4())
    job_dir = temp_jobs_dir / job_id
    (job_dir / "data").mkdir(parents=True, exist_ok=True)
    (job_dir / "provenance").mkdir(parents=True, exist_ok=True)
    (job_dir / "data" / "sample.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (job_dir / "provenance" / "plot1.png").write_bytes(b"png")

    _write_json(
        job_dir / "config.json",
        {
            "job_id": job_id,
            "research_question": "Modern migration test",
            "status": "completed",
            "max_iterations": 5,
            "created_at": "2026-02-01T10:00:00",
            "owner_id": None,
        },
    )

    _write_json(
        job_dir / "knowledge_state.json",
        {
            "config": {
                "job_id": job_id,
                "research_question": "Modern migration test",
                "max_iterations": 5,
                "started_at": "2026-02-01T10:00:00",
            },
            "iteration": 2,
            "hypotheses": [
                {
                    "id": "H001",
                    "iteration_proposed": 1,
                    "statement": "Hypothesis A",
                    "status": "supported",
                    "test_code": "print('ok')",
                    "result": {"conclusion": "supported"},
                }
            ],
            "findings": [
                {
                    "id": "F001",
                    "iteration_discovered": 1,
                    "title": "Finding A",
                    "evidence": "Evidence A",
                    "supporting_hypotheses": ["H001"],
                    "literature_support": ["L001"],
                    "plots": [f"jobs/{job_id}/provenance/plot1.png"],
                    "biological_interpretation": "Interpretation A",
                }
            ],
            "literature": [
                {
                    "id": "L001",
                    "pmid": "123456",
                    "title": "Paper A",
                    "abstract": "Abstract A",
                    "retrieved_at_iteration": 1,
                    "search_query": "query",
                }
            ],
            "analysis_log": [
                {
                    "iteration": 1,
                    "action": "execute_code",
                    "timestamp": "2026-02-01T10:05:00",
                    "code": "print('hello')",
                    "output": "hello",
                }
            ],
            "iteration_summaries": [{"iteration": 1, "summary": "Summary A"}],
            "feedback_history": [{"after_iteration": 1, "text": "Looks good"}],
            "plots": [
                {
                    "iteration": 1,
                    "filename": "plot1.png",
                    "plot_type": "scatter",
                    "description": "Plot A",
                }
            ],
            "consensus_answer": "Consensus A",
        },
    )

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)

    assert result.created_jobs == 1
    assert result.synced_knowledge_state == 1
    assert result.data_files_added == 1
    assert result.plots_added == 1
    assert result.errors == []

    job = (await db_session.execute(select(Job).where(Job.id == UUID(job_id)))).scalar_one()
    assert job.owner_id is None
    assert job.title == "Modern migration test"
    assert job.status == "completed"
    assert job.consensus_answer == "Consensus A"

    assert (
        await db_session.execute(
            select(func.count(Hypothesis.id)).where(Hypothesis.job_id == job.id)
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(select(func.count(Finding.id)).where(Finding.job_id == job.id))
    ).scalar_one() == 1
    assert (
        await db_session.execute(
            select(func.count(Literature.id)).where(Literature.job_id == job.id)
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(
            select(func.count(AnalysisLog.id)).where(AnalysisLog.job_id == job.id)
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(
            select(func.count(IterationSummary.id)).where(IterationSummary.job_id == job.id)
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(
            select(func.count(FeedbackHistory.id)).where(FeedbackHistory.job_id == job.id)
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(
            select(func.count(JobDataFile.id)).where(JobDataFile.job_id == job.id)
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(select(func.count(Plot.id)).where(Plot.job_id == job.id))
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_bootstrap_migrates_legacy_knowledge_state_format(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "shandy.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    job_id = str(uuid4())
    job_dir = temp_jobs_dir / job_id
    (job_dir / "provenance").mkdir(parents=True, exist_ok=True)
    (job_dir / "provenance" / "legacy.png").write_bytes(b"png")

    _write_json(
        job_dir / "config.json",
        {
            "job_id": job_id,
            "research_question": "Legacy migration test",
            "status": "completed",
            "max_iterations": 4,
            "owner_id": None,
        },
    )

    _write_json(
        job_dir / "knowledge_state.json",
        {
            "iteration": 3,
            "hypotheses": [
                {
                    "iteration": 1,
                    "text": "Legacy hypothesis",
                    "status": "confirmed",
                    "rationale": "Legacy rationale",
                }
            ],
            "findings": [
                {
                    "iteration": 2,
                    "content": "Legacy finding",
                    "significance": "high",
                    "evidence": ["Legacy evidence"],
                    "plots": [f"jobs/{job_id}/provenance/legacy.png"],
                }
            ],
            "literature": [
                {
                    "title": "Legacy paper",
                    "authors": "Legacy author",
                    "year": 2024,
                    "doi": "10.1000/legacy",
                    "relevance_score": 0.75,
                }
            ],
            "analysis_log": [
                {
                    "iteration": 2,
                    "action": "load_data",
                    "details": {"file": "legacy.csv"},
                    "status": "success",
                }
            ],
            "feedback_history": [{"iteration": 2, "feedback": "Legacy feedback"}],
            "plots": [
                {
                    "iteration": 2,
                    "filename": "legacy.png",
                    "plot_type": "histogram",
                    "description": "Legacy plot",
                }
            ],
        },
    )

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)

    assert result.created_jobs == 1
    assert result.synced_knowledge_state == 1
    assert result.plots_added == 1
    assert result.errors == []

    job = (await db_session.execute(select(Job).where(Job.id == UUID(job_id)))).scalar_one()

    hyp = (
        await db_session.execute(select(Hypothesis).where(Hypothesis.job_id == job.id))
    ).scalar_one()
    assert hyp.text == "Legacy hypothesis"
    assert hyp.status == "confirmed"

    finding = (
        await db_session.execute(select(Finding).where(Finding.job_id == job.id))
    ).scalar_one()
    assert finding.text == "Legacy finding"
    assert finding.finding_type == "high"

    literature = (
        await db_session.execute(select(Literature).where(Literature.job_id == job.id))
    ).scalar_one()
    assert literature.title == "Legacy paper"
    assert literature.authors == "Legacy author"

    analysis_log = (
        await db_session.execute(select(AnalysisLog).where(AnalysisLog.job_id == job.id))
    ).scalar_one()
    assert analysis_log.action_type == "load_data"

    feedback = (
        await db_session.execute(select(FeedbackHistory).where(FeedbackHistory.job_id == job.id))
    ).scalar_one()
    assert feedback.feedback_text == "Legacy feedback"


@pytest.mark.asyncio
async def test_bootstrap_dry_run_counts_orphaned_jobs(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "shandy.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    job_id = str(uuid4())
    job_dir = temp_jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        job_dir / "config.json",
        {
            "job_id": job_id,
            "research_question": "Dry-run orphan count test",
            "status": "pending",
            "max_iterations": 2,
            "owner_id": None,
        },
    )

    result = await bootstrap_jobs_from_filesystem(
        jobs_dir=temp_jobs_dir,
        dry_run=True,
    )

    assert result.created_jobs == 1
    assert result.orphan_jobs == 1

    job = (await db_session.execute(select(Job).where(Job.id == UUID(job_id)))).scalar_one_or_none()
    assert job is None


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "shandy.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    job_id = str(uuid4())
    job_dir = temp_jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        job_dir / "config.json",
        {
            "job_id": job_id,
            "research_question": "Idempotency test",
            "status": "completed",
            "max_iterations": 3,
            "owner_id": None,
        },
    )

    _write_json(
        job_dir / "knowledge_state.json",
        {
            "config": {
                "job_id": job_id,
                "research_question": "Idempotency test",
                "max_iterations": 3,
            },
            "iteration": 1,
            "hypotheses": [
                {
                    "id": "H001",
                    "iteration_proposed": 1,
                    "statement": "Idempotent hypothesis",
                    "status": "pending",
                }
            ],
            "findings": [],
            "literature": [],
            "analysis_log": [],
            "iteration_summaries": [],
            "feedback_history": [],
        },
    )

    first = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)
    second = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)

    assert first.created_jobs == 1
    assert first.synced_knowledge_state == 1
    assert second.created_jobs == 0
    assert second.existing_jobs == 1
    assert second.synced_knowledge_state == 1
    assert second.errors == []

    assert (await db_session.execute(select(func.count(Job.id)))).scalar_one() == 1
    assert (await db_session.execute(select(func.count(Hypothesis.id)))).scalar_one() == 1


@pytest.mark.asyncio
async def test_bootstrap_migrates_iteration_summaries_for_multiple_jobs(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "shandy.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    job_ids = [str(uuid4()), str(uuid4())]
    for idx, job_id in enumerate(job_ids, start=1):
        job_dir = temp_jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        _write_json(
            job_dir / "config.json",
            {
                "job_id": job_id,
                "research_question": f"Iteration summary migration {idx}",
                "status": "completed",
                "max_iterations": 3,
                "owner_id": None,
            },
        )
        _write_json(
            job_dir / "knowledge_state.json",
            {
                "config": {
                    "job_id": job_id,
                    "research_question": f"Iteration summary migration {idx}",
                    "max_iterations": 3,
                },
                "iteration": 1,
                "hypotheses": [],
                "findings": [],
                "literature": [],
                "analysis_log": [],
                "iteration_summaries": [{"iteration": 1, "summary": f"Summary {idx}"}],
                "feedback_history": [],
            },
        )

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)

    assert result.created_jobs == 2
    assert result.synced_knowledge_state == 2
    assert result.errors == []

    assert (await db_session.execute(select(func.count(IterationSummary.id)))).scalar_one() == 2
