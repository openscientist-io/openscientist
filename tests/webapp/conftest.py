"""Pytest fixtures for webapp tests."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Job, Session, User
from shandy.database.rls import bypass_rls
from shandy.job_manager import JobInfo, JobManager


@pytest.fixture(autouse=True)
def _nicegui_storage_dir(tmp_path):
    """Provide a temporary directory for NiceGUI storage during tests.

    NiceGUI persists user storage to .nicegui/ on disk. Without this,
    teardown fails with FileNotFoundError when async_backup runs.
    """
    storage_dir = tmp_path / ".nicegui"
    storage_dir.mkdir()
    with patch.dict(os.environ, {"NICEGUI_STORAGE_PATH": str(storage_dir)}):
        yield


@pytest_asyncio.fixture
async def webapp_user(db_session: AsyncSession) -> User:
    """Create a test user for webapp tests."""
    async with bypass_rls(db_session):
        user = User(
            email="webapp-test@example.com",
            name="Webapp Test User",
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
    return user


@pytest_asyncio.fixture
async def webapp_session(db_session: AsyncSession, webapp_user: User) -> Session:
    """Create a valid session for the webapp test user."""
    async with bypass_rls(db_session):
        session = Session(
            user_id=webapp_user.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            ip_address="127.0.0.1",
            user_agent="pytest-test-agent",
        )
        db_session.add(session)
        await db_session.commit()
        await db_session.refresh(session)
    return session


@pytest.fixture
def authenticated_storage(webapp_user: User, webapp_session: Session):
    """Provide authenticated storage state for NiceGUI tests.

    Returns the storage dict that should be set in app.storage.user
    for authenticated tests.
    """
    return {
        "authenticated": True,
        "session_token": str(webapp_session.id),
        "user_id": str(webapp_user.id),
        "email": webapp_user.email,
        "name": webapp_user.name,
    }


# Helper functions for creating job file structures


def create_knowledge_state_file(job_dir: Path, data: dict) -> None:
    """Create a knowledge_state.json file in the job directory."""
    job_dir.mkdir(parents=True, exist_ok=True)
    ks_path = job_dir / "knowledge_state.json"
    with open(ks_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def create_config_file(job_dir: Path, data: dict) -> None:
    """Create a config.json file in the job directory."""
    job_dir.mkdir(parents=True, exist_ok=True)
    config_path = job_dir / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def create_report_file(job_dir: Path, content: str) -> None:
    """Create a final_report.md file."""
    job_dir.mkdir(parents=True, exist_ok=True)
    report_path = job_dir / "final_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)


# Database-backed job fixtures


@pytest_asyncio.fixture
async def webapp_job_pending(
    db_session: AsyncSession, webapp_user: User, temp_jobs_dir: Path
) -> tuple[Job, JobInfo, Path]:
    """Create a pending job in the database with file structure."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=webapp_user.id,
            title="What is the effect of gene X on disease Y?",
            status="pending",
            max_iterations=10,
            current_iteration=0,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    job_dir = temp_jobs_dir / str(job.id)

    # Create knowledge state file
    create_knowledge_state_file(
        job_dir,
        {
            "research_question": job.title,
            "iteration": 0,
            "status": "pending",
            "findings": [],
            "literature": [],
            "analysis_log": [],
            "iteration_summaries": [],
        },
    )

    # Create config file
    create_config_file(
        job_dir,
        {
            "job_id": str(job.id),
            "research_question": job.title,
            "status": "pending",
            "max_iterations": job.max_iterations,
            "created_at": job.created_at.isoformat(),
        },
    )

    job_info = JobInfo.from_db_model(job, iterations_completed=0, findings_count=0)
    return job, job_info, job_dir


@pytest_asyncio.fixture
async def webapp_job_running(
    db_session: AsyncSession, webapp_user: User, temp_jobs_dir: Path
) -> tuple[Job, JobInfo, Path]:
    """Create a running job with partial results."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=webapp_user.id,
            title="How does protein A interact with protein B?",
            status="running",
            max_iterations=10,
            current_iteration=2,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    job_dir = temp_jobs_dir / str(job.id)

    # Create knowledge state with findings and literature
    create_knowledge_state_file(
        job_dir,
        {
            "research_question": job.title,
            "iteration": 3,
            "status": "running",
            "findings": [
                {
                    "title": "Protein A binds to domain X of Protein B",
                    "evidence": "Crystal structure shows direct interaction",
                    "biological_interpretation": "This suggests a regulatory mechanism",
                    "iteration_discovered": 1,
                }
            ],
            "literature": [
                {
                    "pmid": "12345678",
                    "title": "Structural analysis of Protein A-B complex",
                    "authors": ["Smith J", "Jones K"],
                    "journal": "Nature Structural Biology",
                    "year": 2024,
                    "abstract": "We determined the crystal structure...",
                    "search_query": "protein A protein B interaction",
                    "retrieved_at_iteration": 1,
                },
                {
                    "pmid": "87654321",
                    "title": "Functional studies of Protein A",
                    "authors": ["Brown M"],
                    "journal": "Cell",
                    "year": 2023,
                    "abstract": "Protein A plays a critical role...",
                    "search_query": "protein A function",
                    "retrieved_at_iteration": 2,
                },
            ],
            "analysis_log": [
                {
                    "iteration": 1,
                    "action": "search_pubmed",
                    "query": "protein A protein B interaction",
                    "results_count": 1,
                },
                {
                    "iteration": 1,
                    "action": "update_knowledge_state",
                    "findings_added": 1,
                },
            ],
            "iteration_summaries": [
                {
                    "iteration": 1,
                    "strapline": "Found direct binding evidence",
                    "summary": "Identified crystal structure showing binding",
                },
                {
                    "iteration": 2,
                    "strapline": "Explored functional implications",
                    "summary": "Reviewed literature on Protein A function",
                },
            ],
        },
    )

    create_config_file(
        job_dir,
        {
            "job_id": str(job.id),
            "research_question": job.title,
            "status": "running",
            "max_iterations": job.max_iterations,
            "created_at": job.created_at.isoformat(),
        },
    )

    job_info = JobInfo.from_db_model(job, iterations_completed=2, findings_count=1)
    return job, job_info, job_dir


@pytest_asyncio.fixture
async def webapp_job_completed(
    db_session: AsyncSession, webapp_user: User, temp_jobs_dir: Path
) -> tuple[Job, JobInfo, Path]:
    """Create a completed job with full results."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=webapp_user.id,
            title="What are the mechanisms of drug resistance in cancer cells?",
            status="completed",
            max_iterations=10,
            current_iteration=5,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    job_dir = temp_jobs_dir / str(job.id)

    create_knowledge_state_file(
        job_dir,
        {
            "research_question": job.title,
            "iteration": 5,
            "status": "completed",
            "findings": [
                {
                    "title": "ABC transporter upregulation",
                    "evidence": "Gene expression shows 5-fold increase",
                    "iteration_discovered": 2,
                },
                {
                    "title": "DNA repair pathway activation",
                    "evidence": "Western blot confirms increased levels",
                    "iteration_discovered": 3,
                },
                {
                    "title": "Apoptosis pathway suppression",
                    "evidence": "Flow cytometry shows reduced markers",
                    "iteration_discovered": 4,
                },
            ],
            "literature": [
                {
                    "pmid": "11111111",
                    "title": "ABC transporters in drug resistance",
                    "authors": ["Lee A", "Kim B"],
                    "journal": "Cancer Research",
                    "year": 2024,
                }
            ],
            "iteration_summaries": [
                {
                    "iteration": 1,
                    "strapline": "Initial survey",
                    "summary": "Reviewed mechanisms",
                },
                {
                    "iteration": 2,
                    "strapline": "ABC transporters",
                    "summary": "Found upregulation",
                },
            ],
        },
    )

    create_config_file(
        job_dir,
        {
            "job_id": str(job.id),
            "research_question": job.title,
            "status": "completed",
            "max_iterations": job.max_iterations,
            "created_at": job.created_at.isoformat(),
        },
    )

    create_report_file(
        job_dir,
        """# Drug Resistance in Cancer Cells

## Summary
This research identified three key mechanisms...

## Findings
1. ABC transporter upregulation
2. DNA repair pathway activation
3. Apoptosis pathway suppression
""",
    )

    job_info = JobInfo.from_db_model(job, iterations_completed=5, findings_count=3)
    return job, job_info, job_dir


@pytest_asyncio.fixture
async def webapp_job_failed(
    db_session: AsyncSession, webapp_user: User, temp_jobs_dir: Path
) -> tuple[Job, JobInfo, Path]:
    """Create a failed job with error."""
    error_msg = '{"type": "error", "error": {"type": "api_error", "message": "API request failed: Rate limit exceeded"}}'

    async with bypass_rls(db_session):
        job = Job(
            owner_id=webapp_user.id,
            title="Test failing question",
            status="failed",
            max_iterations=10,
            current_iteration=1,
            error_message=error_msg,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    job_dir = temp_jobs_dir / str(job.id)

    create_knowledge_state_file(
        job_dir,
        {
            "research_question": job.title,
            "iteration": 1,
            "status": "failed",
            "findings": [],
            "literature": [],
            "analysis_log": [],
        },
    )

    create_config_file(
        job_dir,
        {
            "job_id": str(job.id),
            "research_question": job.title,
            "status": "failed",
            "max_iterations": job.max_iterations,
            "error": error_msg,
            "created_at": job.created_at.isoformat(),
        },
    )

    job_info = JobInfo.from_db_model(job, iterations_completed=1, findings_count=0)
    return job, job_info, job_dir


@pytest_asyncio.fixture
async def webapp_job_awaiting_feedback(
    db_session: AsyncSession, webapp_user: User, temp_jobs_dir: Path
) -> tuple[Job, JobInfo, Path]:
    """Create a job awaiting user feedback (coinvestigate mode)."""
    async with bypass_rls(db_session):
        job = Job(
            owner_id=webapp_user.id,
            title="How do neurons communicate?",
            status="awaiting_feedback",
            max_iterations=10,
            current_iteration=1,
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

    job_dir = temp_jobs_dir / str(job.id)

    create_knowledge_state_file(
        job_dir,
        {
            "research_question": job.title,
            "iteration": 2,
            "status": "awaiting_feedback",
            "findings": [
                {
                    "title": "Synaptic transmission via neurotransmitters",
                    "evidence": "Literature confirms chemical signaling",
                    "iteration_discovered": 1,
                }
            ],
            "literature": [],
            "iteration_summaries": [
                {
                    "iteration": 1,
                    "strapline": "Explored basic mechanisms",
                    "summary": "Found evidence of chemical synaptic transmission",
                }
            ],
        },
    )

    create_config_file(
        job_dir,
        {
            "job_id": str(job.id),
            "research_question": job.title,
            "status": "awaiting_feedback",
            "max_iterations": job.max_iterations,
            "investigation_mode": "coinvestigate",
            "awaiting_feedback_since": datetime.now(timezone.utc).isoformat(),
            "created_at": job.created_at.isoformat(),
        },
    )

    job_info = JobInfo.from_db_model(job, iterations_completed=1, findings_count=1)
    return job, job_info, job_dir


@pytest.fixture
def job_manager(temp_jobs_dir: Path) -> JobManager:
    """Create a real JobManager instance for testing."""
    return JobManager(jobs_dir=temp_jobs_dir, max_concurrent=1)


# Provider fixtures - we still need to mock external API calls


class TestCostInfo:
    """Test cost info for provider mocking."""

    def __init__(self):
        self.total_spend_usd = 15.50
        self.recent_spend_usd = 2.25
        self.recent_period_hours = 24
        self.budget_remaining_usd = 84.50
        self.provider_name = "test"
        self.description = "Test provider costs"
        self.data_lag_note = None


@pytest.fixture
def mock_provider_cost_info():
    """Mock provider that returns cost info without external calls."""
    from unittest.mock import MagicMock

    provider = MagicMock()
    provider.name = "test"
    provider.get_cost_info.return_value = TestCostInfo()
    provider.check_budget_limits.return_value = {
        "can_proceed": True,
        "within_budget": True,
        "warnings": [],
        "errors": [],
    }
    return provider
