"""
Tests for core database functionality.

Tests basic CRUD operations, model relationships, and UUIDv7 generation.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import (
    AnalysisLog,
    APIKey,
    CostRecord,
    Finding,
    Hypothesis,
    Job,
    JobChatMessage,
    JobShare,
    Literature,
    OAuthAccount,
    Plot,
    User,
)
from openscientist.database.models import (
    Session as DBSession,
)


@pytest.mark.asyncio
async def test_user_creation(db_session: AsyncSession):
    """Test creating a user with UUIDv7 ID generation."""
    user = User(
        email="newuser@example.com",
        name="New User",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    # Verify UUID was generated
    assert isinstance(user.id, UUID)
    assert user.email == "newuser@example.com"
    assert user.name == "New User"
    assert user.is_active is True
    assert user.is_approved is False
    assert isinstance(user.created_at, datetime)


@pytest.mark.asyncio
async def test_job_creation_with_owner(db_session: AsyncSession, test_user: User):
    """Test creating a job with an owner."""
    job = Job(
        owner_id=test_user.id,
        research_question="Research Job",
        description="Testing job creation",
        status="running",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Verify job attributes
    assert isinstance(job.id, UUID)
    assert job.owner_id == test_user.id
    assert job.research_question == "Research Job"
    assert job.status == "running"
    assert isinstance(job.created_at, datetime)

    # Verify relationship
    await db_session.refresh(test_user, ["jobs"])
    assert len(test_user.jobs) == 1
    assert test_user.jobs[0].id == job.id


@pytest.mark.asyncio
async def test_orphaned_job_creation(db_session: AsyncSession):
    """Test creating a job without an owner (orphaned job)."""
    job = Job(
        owner_id=None,
        research_question="Orphaned Job",
        description="Job without owner",
        status="pending",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.owner_id is None
    assert job.research_question == "Orphaned Job"


@pytest.mark.asyncio
async def test_api_key_creation(db_session: AsyncSession, test_user: User):
    """Test creating an API key for a user."""
    api_key = APIKey(
        user_id=test_user.id,
        name="Production Key",
        key_hash="hashed_secret_value",
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)

    assert isinstance(api_key.id, UUID)
    assert api_key.user_id == test_user.id
    assert api_key.name == "Production Key"
    assert api_key.is_active is True

    # Verify relationship
    await db_session.refresh(test_user, ["api_keys"])
    assert len(test_user.api_keys) >= 1


@pytest.mark.asyncio
async def test_session_creation(db_session: AsyncSession, test_user: User):
    """Test creating a user session."""
    expires_at = datetime.now(UTC) + timedelta(days=7)
    session = DBSession(
        user_id=test_user.id,
        expires_at=expires_at,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    assert isinstance(session.id, UUID)
    assert session.user_id == test_user.id


@pytest.mark.asyncio
async def test_oauth_account_creation(db_session: AsyncSession, test_user: User):
    """Test creating an OAuth account linked to a user."""
    oauth_account = OAuthAccount(
        user_id=test_user.id,
        provider="github",
        provider_user_id="github_12345",
        email="test@example.com",
        name="Test User",
        access_token="github_access_token",
    )
    db_session.add(oauth_account)
    await db_session.commit()
    await db_session.refresh(oauth_account)

    assert isinstance(oauth_account.id, UUID)
    assert oauth_account.provider == "github"
    assert oauth_account.provider_user_id == "github_12345"

    # Verify relationship
    await db_session.refresh(test_user, ["oauth_accounts"])
    assert len(test_user.oauth_accounts) == 1


@pytest.mark.asyncio
async def test_job_share_creation(db_session: AsyncSession, test_user: User, test_user2: User):
    """Test creating a job share between two users."""
    # Create a job owned by test_user
    job = Job(
        owner_id=test_user.id,
        research_question="Shared Job",
        description="Job to be shared",
        status="completed",
    )
    db_session.add(job)
    await db_session.commit()

    # Share job with test_user2
    job_share = JobShare(
        job_id=job.id,
        shared_with_user_id=test_user2.id,
        permission_level="view",
    )
    db_session.add(job_share)
    await db_session.commit()
    await db_session.refresh(job_share)

    assert isinstance(job_share.id, UUID)
    assert job_share.job_id == job.id
    assert job_share.shared_with_user_id == test_user2.id
    assert job_share.permission_level == "view"


@pytest.mark.asyncio
async def test_hypothesis_finding_relationship(db_session: AsyncSession, test_job: Job):
    """Test creating hypotheses and findings with relationships."""
    # Create hypothesis
    hypothesis = Hypothesis(
        job_id=test_job.id,
        iteration=1,
        text="Test hypothesis content",
        status="active",
    )
    db_session.add(hypothesis)
    await db_session.commit()
    await db_session.refresh(hypothesis)
    hypothesis_id = hypothesis.id

    # Create finding
    finding = Finding(
        job_id=test_job.id,
        iteration=1,
        text="Test finding content",
        finding_type="observation",
        source="code_execution",
    )
    db_session.add(finding)
    await db_session.commit()
    await db_session.refresh(finding)
    finding_id = finding.id

    # Link finding to hypothesis via direct junction table insert
    await db_session.execute(
        text("INSERT INTO finding_hypotheses (finding_id, hypothesis_id) VALUES (:fid, :hid)"),
        {"fid": finding.id, "hid": hypothesis.id},
    )
    await db_session.commit()

    # Verify relationship - eagerly load within same context
    await db_session.refresh(hypothesis, ["findings"])
    await db_session.refresh(finding, ["hypotheses"])

    assert len(hypothesis.findings) == 1
    assert len(finding.hypotheses) == 1
    assert hypothesis.findings[0].id == finding_id
    assert finding.hypotheses[0].id == hypothesis_id


@pytest.mark.asyncio
async def test_literature_creation(db_session: AsyncSession, test_job: Job):
    """Test creating literature records."""
    literature = Literature(
        job_id=test_job.id,
        iteration=1,
        title="Important Paper",
        authors="Smith et al.",
        year=2026,
        doi="10.1234/test.2026",
        abstract="This is a test abstract",
        relevance_score=0.95,
    )
    db_session.add(literature)
    await db_session.commit()
    await db_session.refresh(literature)

    assert isinstance(literature.id, UUID)
    assert literature.title == "Important Paper"
    assert literature.relevance_score == 0.95


@pytest.mark.asyncio
async def test_analysis_log_creation(db_session: AsyncSession, test_job: Job):
    """Test creating analysis log entries."""
    log = AnalysisLog(
        job_id=test_job.id,
        iteration=1,
        step_number=1,
        action_type="code_execution",
        description="Executed analysis code",
        input_data={"code": "print('hello')"},
        output_data={"output": "hello"},
        success=True,
    )
    db_session.add(log)
    await db_session.commit()
    await db_session.refresh(log)

    assert isinstance(log.id, UUID)
    assert log.action_type == "code_execution"
    assert log.success is True
    assert log.output_data is not None and log.output_data["output"] == "hello"


@pytest.mark.asyncio
async def test_plot_creation(db_session: AsyncSession, test_job: Job):
    """Test creating plot metadata."""
    plot = Plot(
        job_id=test_job.id,
        iteration=1,
        file_path="/jobs/test_job/plots/scatter_plot.png",
        plot_type="scatter",
        title="Test Scatter Plot",
        description="A test plot",
    )
    db_session.add(plot)
    await db_session.commit()
    await db_session.refresh(plot)

    assert isinstance(plot.id, UUID)
    assert plot.title == "Test Scatter Plot"
    assert plot.plot_type == "scatter"


@pytest.mark.asyncio
async def test_chat_message_creation(db_session: AsyncSession, test_job: Job, test_user: User):
    """Test creating chat messages."""
    _ = test_user
    message = JobChatMessage(
        job_id=test_job.id,
        role="user",
        content="What does this plot show?",
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    assert isinstance(message.id, UUID)
    assert message.role == "user"
    assert message.content == "What does this plot show?"


@pytest.mark.asyncio
async def test_cost_record_creation(db_session: AsyncSession, test_job: Job):
    """Test creating cost records."""
    cost = CostRecord(
        job_id=test_job.id,
        iteration=1,
        operation_type="analysis",
        provider="vertex",
        model="claude-3-5-sonnet",
        input_tokens=1000,
        output_tokens=500,
        cost_usd=0.015,
    )
    db_session.add(cost)
    await db_session.commit()
    await db_session.refresh(cost)

    assert isinstance(cost.id, UUID)
    assert cost.input_tokens == 1000
    assert cost.output_tokens == 500
    assert cost.cost_usd == 0.015


@pytest.mark.asyncio
async def test_cascade_delete_job(db_session: AsyncSession, test_user: User):
    """Test that deleting a job cascades to related records."""
    # Create job with related records
    job = Job(
        owner_id=test_user.id,
        research_question="Job to Delete",
        description="Will be deleted",
        status="completed",
    )
    db_session.add(job)
    await db_session.commit()

    # Add related records
    hypothesis = Hypothesis(job_id=job.id, iteration=1, text="Test", status="active")
    finding = Finding(
        job_id=job.id,
        iteration=1,
        text="Test",
        finding_type="observation",
        source="code_execution",
    )
    plot = Plot(job_id=job.id, iteration=1, file_path="/test.png", title="Test Plot")

    db_session.add_all([hypothesis, finding, plot])
    await db_session.commit()

    # Delete job
    await db_session.delete(job)
    await db_session.commit()

    # Verify related records are also deleted
    hypothesis_result = await db_session.execute(
        select(Hypothesis).where(Hypothesis.job_id == job.id)
    )
    assert len(hypothesis_result.scalars().all()) == 0

    finding_result = await db_session.execute(select(Finding).where(Finding.job_id == job.id))
    assert len(finding_result.scalars().all()) == 0


@pytest.mark.asyncio
async def test_uuidv7_time_ordering(db_session: AsyncSession):
    """Test that UUIDv7 IDs are time-ordered."""
    # Create multiple users in sequence
    user1 = User(email="user1@test.com", name="User 1")
    db_session.add(user1)
    await db_session.commit()
    await db_session.refresh(user1)

    user2 = User(email="user2@test.com", name="User 2")
    db_session.add(user2)
    await db_session.commit()
    await db_session.refresh(user2)

    user3 = User(email="user3@test.com", name="User 3")
    db_session.add(user3)
    await db_session.commit()
    await db_session.refresh(user3)

    # UUIDv7 should be ordered by creation time
    assert user1.id < user2.id < user3.id

    # Timestamps should also be ordered
    assert user1.created_at <= user2.created_at <= user3.created_at
