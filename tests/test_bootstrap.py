"""Tests for legacy filesystem bootstrap into the database."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

import openscientist.bootstrap as bootstrap_module
from openscientist.bootstrap import bootstrap_jobs_from_filesystem
from openscientist.database.models import (
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


def test_derive_uuidv7_seed_time_from_filesystem_uses_earliest_fallback_timestamp(temp_jobs_dir):
    job_dir = temp_jobs_dir / "job_seed_fallback"
    job_dir.mkdir(parents=True, exist_ok=True)
    older_file = job_dir / "older.txt"
    newer_file = job_dir / "newer.txt"
    older_file.write_text("old", encoding="utf-8")
    newer_file.write_text("new", encoding="utf-8")

    older_epoch = 1_700_000_000
    newer_epoch = 1_700_001_000
    os.utime(older_file, (older_epoch, older_epoch))
    os.utime(newer_file, (newer_epoch, newer_epoch))

    seed_time = bootstrap_module._derive_uuidv7_seed_time_from_filesystem(job_dir)

    assert seed_time == datetime.fromtimestamp(older_epoch, tz=UTC)


@pytest.mark.asyncio
async def test_generate_uuidv7_uses_seed_time_when_provided(db_session: AsyncSession):
    seed_time = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    generated = await bootstrap_module._generate_uuidv7(db_session, seed_time=seed_time)
    extracted = (
        await db_session.execute(
            text("SELECT uuid_extract_timestamp(CAST(:uuid_value AS uuid))"),
            {"uuid_value": str(generated)},
        )
    ).scalar_one()

    assert extracted is not None
    normalized = extracted.astimezone(UTC) if extracted.tzinfo else extracted.replace(tzinfo=UTC)
    assert abs((normalized - seed_time).total_seconds()) < 1


@pytest.mark.asyncio
async def test_bootstrap_creates_job_and_syncs_modern_knowledge_state(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
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
        "openscientist.bootstrap.get_admin_session",
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
async def test_bootstrap_migrates_non_uuid_legacy_folder_and_payload_ids(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )
    migrated_uuid = UUID("11111111-1111-4111-8111-111111111111")

    async def _fake_uuidv7(_session: AsyncSession, seed_time: datetime | None = None) -> UUID:
        _ = seed_time
        return migrated_uuid

    monkeypatch.setattr("openscientist.bootstrap._generate_uuidv7", _fake_uuidv7)

    legacy_id = "job_deadbeef"
    legacy_dir = temp_jobs_dir / legacy_id
    (legacy_dir / "data").mkdir(parents=True, exist_ok=True)
    (legacy_dir / "provenance").mkdir(parents=True, exist_ok=True)
    (legacy_dir / "data" / "sample.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    (legacy_dir / "provenance" / "plot.png").write_bytes(b"png")

    _write_json(
        legacy_dir / "config.json",
        {
            "job_id": legacy_id,
            "research_question": "Legacy non-UUID migration test",
            "status": "completed",
            "owner_id": None,
        },
    )
    _write_json(
        legacy_dir / "knowledge_state.json",
        {
            "config": {
                "job_id": legacy_id,
                "research_question": "Legacy non-UUID migration test",
                "max_iterations": 3,
            },
            "iteration": 1,
            "hypotheses": [],
            "findings": [
                {
                    "id": "F001",
                    "iteration_discovered": 1,
                    "title": "Legacy finding",
                    "plots": [f"jobs/{legacy_id}/provenance/plot.png"],
                }
            ],
            "literature": [],
            "analysis_log": [],
            "iteration_summaries": [],
            "feedback_history": [],
            "plots": [
                {
                    "iteration": 1,
                    "file_path": f"jobs/{legacy_id}/provenance/plot.png",
                    "description": "Legacy plot",
                }
            ],
        },
    )

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)

    assert result.created_jobs == 1
    assert result.synced_knowledge_state == 1
    assert result.deleted_knowledge_state_files == 1
    assert result.skipped_invalid_job_id == 0
    assert result.errors == []

    migrated_dir = temp_jobs_dir / str(migrated_uuid)
    assert migrated_dir.exists()
    assert not legacy_dir.exists()

    with open(migrated_dir / "config.json", encoding="utf-8") as f:
        migrated_config = json.load(f)

    assert migrated_config["job_id"] == str(migrated_uuid)
    assert not (migrated_dir / "knowledge_state.json").exists()

    job = (await db_session.execute(select(Job).where(Job.id == migrated_uuid))).scalar_one()
    assert job.owner_id is None

    plot_paths = (
        await db_session.execute(select(Plot.file_path).where(Plot.job_id == migrated_uuid))
    ).scalars()
    assert list(plot_paths) == ["provenance/plot.png"]


@pytest.mark.asyncio
async def test_bootstrap_dry_run_non_uuid_legacy_does_not_rename_or_write(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )
    migrated_uuid = UUID("22222222-2222-4222-8222-222222222222")

    async def _fake_uuidv7(_session: AsyncSession, seed_time: datetime | None = None) -> UUID:
        _ = seed_time
        return migrated_uuid

    monkeypatch.setattr("openscientist.bootstrap._generate_uuidv7", _fake_uuidv7)

    legacy_id = "job_cafebabe"
    legacy_dir = temp_jobs_dir / legacy_id
    legacy_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        legacy_dir / "config.json",
        {
            "job_id": legacy_id,
            "research_question": "Dry-run legacy migration",
            "status": "pending",
            "owner_id": None,
        },
    )

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir, dry_run=True)

    assert result.created_jobs == 1
    assert result.skipped_invalid_job_id == 0
    assert result.errors == []
    assert legacy_dir.exists()
    assert not (temp_jobs_dir / str(migrated_uuid)).exists()

    with open(legacy_dir / "config.json", encoding="utf-8") as f:
        config = json.load(f)
    assert config["job_id"] == legacy_id

    job = (
        await db_session.execute(select(Job).where(Job.id == migrated_uuid))
    ).scalar_one_or_none()
    assert job is None


@pytest.mark.asyncio
async def test_bootstrap_migrates_when_config_missing_but_ks_has_legacy_id(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )
    migrated_uuid = UUID("33333333-3333-4333-8333-333333333333")

    async def _fake_uuidv7(_session: AsyncSession, seed_time: datetime | None = None) -> UUID:
        _ = seed_time
        return migrated_uuid

    monkeypatch.setattr("openscientist.bootstrap._generate_uuidv7", _fake_uuidv7)

    legacy_id = "job_feedface"
    legacy_dir = temp_jobs_dir / legacy_id
    legacy_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        legacy_dir / "knowledge_state.json",
        {
            "config": {
                "job_id": legacy_id,
                "research_question": "KS-only migration",
                "max_iterations": 2,
            },
            "iteration": 1,
            "hypotheses": [],
            "findings": [],
            "literature": [],
            "analysis_log": [],
            "iteration_summaries": [],
            "feedback_history": [],
        },
    )

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)

    assert result.created_jobs == 1
    assert result.errors == []
    assert (temp_jobs_dir / str(migrated_uuid)).exists()
    assert not legacy_dir.exists()

    job = (await db_session.execute(select(Job).where(Job.id == migrated_uuid))).scalar_one()
    assert job.title == "KS-only migration"


@pytest.mark.asyncio
async def test_bootstrap_seeds_uuidv7_time_from_filesystem(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    expected_seed = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    observed_seed_times: list[datetime | None] = []
    migrated_uuid = UUID("44444444-4444-4444-8444-444444444444")

    def _fake_seed(_job_dir) -> datetime:
        return expected_seed

    async def _fake_uuidv7(_session: AsyncSession, seed_time: datetime | None = None) -> UUID:
        observed_seed_times.append(seed_time)
        return migrated_uuid

    monkeypatch.setattr(
        "openscientist.bootstrap._derive_uuidv7_seed_time_from_filesystem",
        _fake_seed,
    )
    monkeypatch.setattr("openscientist.bootstrap._generate_uuidv7", _fake_uuidv7)

    legacy_id = "job_seeded_time"
    legacy_dir = temp_jobs_dir / legacy_id
    legacy_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        legacy_dir / "config.json",
        {
            "job_id": legacy_id,
            "research_question": "Filesystem seed test",
            "status": "pending",
            "owner_id": None,
        },
    )

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir, dry_run=True)

    assert result.created_jobs == 1
    assert result.errors == []
    assert observed_seed_times == [expected_seed]


@pytest.mark.asyncio
async def test_bootstrap_offsets_colliding_seed_times_deterministically(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    base_seed = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
    observed_seed_times: list[datetime | None] = []
    generated_ids = iter(
        [
            UUID("55555555-5555-4555-8555-555555555555"),
            UUID("66666666-6666-4666-8666-666666666666"),
        ]
    )

    def _fake_seed(_job_dir) -> datetime:
        return base_seed

    async def _fake_uuidv7(_session: AsyncSession, seed_time: datetime | None = None) -> UUID:
        observed_seed_times.append(seed_time)
        return next(generated_ids)

    monkeypatch.setattr(
        "openscientist.bootstrap._derive_uuidv7_seed_time_from_filesystem",
        _fake_seed,
    )
    monkeypatch.setattr("openscientist.bootstrap._generate_uuidv7", _fake_uuidv7)

    for legacy_id in ("job_a", "job_b"):
        legacy_dir = temp_jobs_dir / legacy_id
        legacy_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            legacy_dir / "config.json",
            {
                "job_id": legacy_id,
                "research_question": f"Collision test {legacy_id}",
                "status": "pending",
                "owner_id": None,
            },
        )

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir, dry_run=True)

    assert result.created_jobs == 2
    assert result.errors == []
    assert observed_seed_times == [base_seed, base_seed + timedelta(milliseconds=1)]


@pytest.mark.asyncio
async def test_bootstrap_skips_when_config_and_ks_are_both_invalid_json(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
        fake_admin_session(db_session),
    )

    legacy_dir = temp_jobs_dir / "job_badjson"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "config.json").write_text("{bad", encoding="utf-8")
    (legacy_dir / "knowledge_state.json").write_text("{bad", encoding="utf-8")

    result = await bootstrap_jobs_from_filesystem(jobs_dir=temp_jobs_dir)

    assert result.created_jobs == 0
    assert result.skipped_empty_directory == 1
    assert len(result.errors) == 2
    assert (await db_session.execute(select(func.count(Job.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_bootstrap_dry_run_counts_orphaned_jobs(
    db_session: AsyncSession,
    temp_jobs_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "openscientist.bootstrap.get_admin_session",
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
        "openscientist.bootstrap.get_admin_session",
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
    assert first.deleted_knowledge_state_files == 1
    assert second.created_jobs == 0
    assert second.existing_jobs == 1
    assert second.synced_knowledge_state == 0
    assert second.deleted_knowledge_state_files == 0
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
        "openscientist.bootstrap.get_admin_session",
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
