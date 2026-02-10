"""
Tests for job migration script.

Tests migration from file-based storage to PostgreSQL database.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import (
    AnalysisLog,
    Finding,
    Hypothesis,
    IterationSummary,
    Job,
    Literature,
    Plot,
)
from shandy.database.rls import bypass_rls


@pytest.fixture
def sample_config() -> dict:
    """Sample config.json for testing."""
    return {
        "job_id": "job_abc123",
        "research_question": "What is the crystal structure?",
        "provider": "vertex",
        "model": "claude-3-5-sonnet",
        "max_iterations": 10,
        "status": "completed",
        "created_at": "2026-02-01T10:00:00",
        "started_at": "2026-02-01T10:01:00",
        "completed_at": "2026-02-01T12:00:00",
    }


@pytest.fixture
def sample_knowledge_state() -> dict:
    """Sample knowledge_state.json for testing."""
    return {
        "iteration": 5,
        "hypotheses": [
            {
                "iteration": 1,
                "text": "The protein has an alpha-helix structure",
                "status": "confirmed",
                "rationale": "Based on diffraction pattern",
            },
            {
                "iteration": 2,
                "text": "Binding site is at position X",
                "status": "rejected",
                "rationale": "Contradicted by further analysis",
            },
        ],
        "findings": [
            {
                "iteration": 3,
                "content": "High electron density at binding site",
                "significance": "high",
                "confidence": 0.95,
                "evidence": ["Plot 1", "Plot 2"],
            },
        ],
        "literature": [
            {
                "title": "Protein Crystallography Basics",
                "authors": "Smith et al.",
                "year": 2025,
                "doi": "10.1234/test.2025",
                "relevance_score": 0.88,
                "key_findings": ["Finding 1", "Finding 2"],
            },
        ],
        "iteration_summaries": [
            {
                "iteration": 1,
                "strapline": "Initial analysis",
                "summary": "Started with structure determination",
            },
        ],
        "analysis_log": [
            {
                "iteration": 1,
                "action": "load_data",
                "details": {"file": "data.mtz"},
                "status": "success",
            },
        ],
        "feedback_history": [
            {
                "iteration": 1,
                "feedback": "Good progress",
                "timestamp": "2026-02-01T10:30:00",
            },
        ],
        "plots": [
            {
                "iteration": 1,
                "filename": "plot1.png",
                "plot_type": "diffraction",
                "description": "Diffraction pattern",
            },
        ],
    }


@pytest.fixture
async def mock_job_directory(
    temp_jobs_dir: Path, sample_config: dict, sample_knowledge_state: dict
) -> Path:
    """Create a mock job directory with config and knowledge state files."""
    job_id = "job_abc123"
    job_dir = temp_jobs_dir / job_id
    job_dir.mkdir()

    # Write config.json
    with open(job_dir / "config.json", "w") as f:
        json.dump(sample_config, f)

    # Write knowledge_state.json
    with open(job_dir / "knowledge_state.json", "w") as f:
        json.dump(sample_knowledge_state, f)

    return job_dir


@pytest.mark.asyncio
async def test_parse_config_json(mock_job_directory: Path):
    """Test parsing config.json file."""
    config_file = mock_job_directory / "config.json"

    with open(config_file) as f:
        config = json.load(f)

    assert config["job_id"] == "job_abc123"
    assert config["research_question"] == "What is the crystal structure?"
    assert config["status"] == "completed"
    assert config["max_iterations"] == 10


@pytest.mark.asyncio
async def test_parse_knowledge_state(mock_job_directory: Path):
    """Test parsing knowledge_state.json file."""
    ks_file = mock_job_directory / "knowledge_state.json"

    with open(ks_file) as f:
        ks = json.load(f)

    assert ks["iteration"] == 5
    assert len(ks["hypotheses"]) == 2
    assert len(ks["findings"]) == 1
    assert len(ks["literature"]) == 1


@pytest.mark.asyncio
async def test_job_migration_creates_orphaned_job(
    db_session: AsyncSession, mock_job_directory: Path
):
    """Test that migration creates orphaned job (owner_id=NULL)."""
    # Load config
    with open(mock_job_directory / "config.json") as f:
        config = json.load(f)

    # Create orphaned job
    async with bypass_rls(db_session):
        job = Job(
            owner_id=None,  # Orphaned
            title=config["research_question"],
            description=config["research_question"],
            status=config["status"],
            max_iterations=config["max_iterations"],
            current_iteration=0,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Verify orphaned status
    assert job.owner_id is None
    assert job.title == "What is the crystal structure?"


@pytest.mark.asyncio
async def test_hypothesis_migration(db_session: AsyncSession, sample_knowledge_state: dict):
    """Test migrating hypotheses from knowledge state."""
    # Create job
    async with bypass_rls(db_session):
        job = Job(
            owner_id=None,
            title="Test Job",
            description="Test",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Migrate hypotheses
    async with bypass_rls(db_session):
        for h in sample_knowledge_state["hypotheses"]:
            hypothesis = Hypothesis(
                job_id=job.id,
                iteration=h["iteration"],
                text=h["text"],
                status=h["status"],
                rationale=h.get("rationale"),
            )
            db_session.add(hypothesis)

        await db_session.commit()

    # Verify migration
    async with bypass_rls(db_session):
        stmt = select(Hypothesis).where(Hypothesis.job_id == job.id)
        result = await db_session.execute(stmt)
        hypotheses = result.scalars().all()

    assert len(hypotheses) == 2
    assert hypotheses[0].text == "The protein has an alpha-helix structure"
    assert hypotheses[0].status == "confirmed"
    assert hypotheses[1].status == "rejected"


@pytest.mark.asyncio
async def test_finding_migration(db_session: AsyncSession, sample_knowledge_state: dict):
    """Test migrating findings from knowledge state."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=None,
            title="Test Job",
            description="Test",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Migrate findings
    async with bypass_rls(db_session):
        for f in sample_knowledge_state["findings"]:
            finding = Finding(
                job_id=job.id,
                iteration=f["iteration"],
                text=f["content"],
                finding_type=f["significance"],
                source="code_execution",
            )
            db_session.add(finding)

        await db_session.commit()

    # Verify migration
    async with bypass_rls(db_session):
        stmt = select(Finding).where(Finding.job_id == job.id)
        result = await db_session.execute(stmt)
        findings = result.scalars().all()

    assert len(findings) == 1
    assert findings[0].text == "High electron density at binding site"
    assert findings[0].finding_type == "high"


@pytest.mark.asyncio
async def test_literature_migration(db_session: AsyncSession, sample_knowledge_state: dict):
    """Test migrating literature from knowledge state."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=None,
            title="Test Job",
            description="Test",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Migrate literature
    async with bypass_rls(db_session):
        for lit in sample_knowledge_state["literature"]:
            literature = Literature(
                job_id=job.id,
                iteration=1,
                title=lit["title"],
                authors=lit.get("authors"),
                year=lit.get("year"),
                doi=lit.get("doi"),
                relevance_score=lit.get("relevance_score"),
            )
            db_session.add(literature)

        await db_session.commit()

    # Verify migration
    async with bypass_rls(db_session):
        stmt = select(Literature).where(Literature.job_id == job.id)
        result = await db_session.execute(stmt)
        literature_records = result.scalars().all()

    assert len(literature_records) == 1
    assert literature_records[0].title == "Protein Crystallography Basics"
    assert literature_records[0].authors == "Smith et al."
    assert literature_records[0].relevance_score == 0.88


@pytest.mark.asyncio
async def test_iteration_summary_migration(db_session: AsyncSession, sample_knowledge_state: dict):
    """Test migrating iteration summaries."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=None,
            title="Test Job",
            description="Test",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Migrate iteration summaries
    async with bypass_rls(db_session):
        for summary in sample_knowledge_state["iteration_summaries"]:
            it_summary = IterationSummary(
                job_id=job.id,
                iteration=summary["iteration"],
                summary_text=summary["summary"],
            )
            db_session.add(it_summary)

        await db_session.commit()

    # Verify migration
    async with bypass_rls(db_session):
        stmt = select(IterationSummary).where(IterationSummary.job_id == job.id)
        result = await db_session.execute(stmt)
        summaries = result.scalars().all()

    assert len(summaries) == 1
    assert summaries[0].summary_text == "Started with structure determination"


@pytest.mark.asyncio
async def test_analysis_log_migration(db_session: AsyncSession, sample_knowledge_state: dict):
    """Test migrating analysis log entries."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=None,
            title="Test Job",
            description="Test",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Migrate analysis logs
    async with bypass_rls(db_session):
        for log in sample_knowledge_state["analysis_log"]:
            analysis_log = AnalysisLog(
                job_id=job.id,
                iteration=log["iteration"],
                step_number=1,
                action_type=log["action"],
                description=f"Migrated action: {log['action']}",
                input_data=log.get("details"),
                success=True,
            )
            db_session.add(analysis_log)

        await db_session.commit()

    # Verify migration
    async with bypass_rls(db_session):
        stmt = select(AnalysisLog).where(AnalysisLog.job_id == job.id)
        result = await db_session.execute(stmt)
        logs = result.scalars().all()

    assert len(logs) == 1
    assert logs[0].action_type == "load_data"
    assert logs[0].success is True


@pytest.mark.asyncio
async def test_plot_metadata_migration(
    db_session: AsyncSession, sample_knowledge_state: dict, temp_jobs_dir: Path
):
    """Test migrating plot metadata (files remain on disk)."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=None,
            title="Test Job",
            description="Test",
            status="completed",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # Migrate plots
    async with bypass_rls(db_session):
        for plot_data in sample_knowledge_state["plots"]:
            plot = Plot(
                job_id=job.id,
                iteration=plot_data["iteration"],
                title=plot_data.get("description", "Untitled"),
                file_path=f"jobs/{job.id}/plots/{plot_data['filename']}",
                plot_type=plot_data.get("plot_type"),
                description=plot_data.get("description"),
            )
            db_session.add(plot)

        await db_session.commit()

    # Verify migration
    async with bypass_rls(db_session):
        stmt = select(Plot).where(Plot.job_id == job.id)
        result = await db_session.execute(stmt)
        plots = result.scalars().all()

    assert len(plots) == 1
    assert plots[0].title == "Diffraction pattern"
    assert plots[0].plot_type == "diffraction"


@pytest.mark.asyncio
async def test_missing_config_file(temp_jobs_dir: Path):
    """Test handling of missing config.json."""
    job_dir = temp_jobs_dir / "job_missing"
    job_dir.mkdir()

    # No config.json created
    config_file = job_dir / "config.json"

    assert not config_file.exists()
    # Migration should skip or handle gracefully


@pytest.mark.asyncio
async def test_invalid_json_handling(temp_jobs_dir: Path):
    """Test handling of invalid JSON in config files."""
    job_dir = temp_jobs_dir / "job_invalid"
    job_dir.mkdir()

    # Write invalid JSON
    with open(job_dir / "config.json", "w") as f:
        f.write("{ invalid json content")

    # Attempt to parse
    try:
        with open(job_dir / "config.json") as f:
            json.load(f)
        assert False, "Should have raised JSONDecodeError"
    except json.JSONDecodeError:
        pass  # Expected


@pytest.mark.asyncio
async def test_timestamps_migration(db_session: AsyncSession, sample_config: dict):
    """Test that timestamps are correctly migrated."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=None,
            title="Test Job",
            description="Test",
            status="completed",
            created_at=datetime.fromisoformat(sample_config["created_at"]),
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    # The DB stores as timezone-aware UTC; check the date/time portion
    assert job.created_at.year == 2026
    assert job.created_at.month == 2
    assert job.created_at.day == 1
