"""
Tests for job chat functionality.

Tests chat message creation, conversation history, and context loading.
"""

import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Job, JobChatMessage, User
from shandy.database.rls import set_current_user
from shandy.job_chat import get_chat_history, load_job_context
from tests.helpers import enable_rls


@pytest.mark.asyncio
async def test_create_chat_message(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test creating a chat message."""
    message = JobChatMessage(
        job_id=test_job.id,
        role="user",
        content="What are the main findings?",
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)

    assert isinstance(message.id, UUID)
    assert message.job_id == test_job.id
    assert message.role == "user"
    assert message.content == "What are the main findings?"
    assert isinstance(message.created_at, datetime)


@pytest.mark.asyncio
async def test_chat_conversation_flow(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test a full conversation flow with user and assistant messages."""
    # User asks a question
    user_msg = JobChatMessage(
        job_id=test_job.id,
        role="user",
        content="Can you explain the first hypothesis?",
    )
    db_session.add(user_msg)
    await db_session.commit()

    # Assistant responds
    assistant_msg = JobChatMessage(
        job_id=test_job.id,
        role="assistant",
        content="The first hypothesis suggests...",
    )
    db_session.add(assistant_msg)
    await db_session.commit()

    # Query conversation
    stmt = (
        select(JobChatMessage)
        .where(JobChatMessage.job_id == test_job.id)
        .order_by(JobChatMessage.created_at)
    )
    result = await db_session.execute(stmt)
    messages = result.scalars().all()

    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"


@pytest.mark.asyncio
async def test_get_chat_history(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test retrieving chat history."""
    # Create multiple messages
    messages = [
        JobChatMessage(
            job_id=test_job.id,
            role="user",
            content=f"Question {i}",
        )
        for i in range(5)
    ]

    for msg in messages:
        db_session.add(msg)
    await db_session.commit()

    # Retrieve history
    history = await get_chat_history(db_session, test_job.id, limit=10)

    assert len(history) == 5
    assert all(msg.job_id == test_job.id for msg in history)

    # Should be chronologically ordered
    for i in range(len(history) - 1):
        assert history[i].created_at <= history[i + 1].created_at


@pytest.mark.asyncio
async def test_chat_history_limit(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test that chat history respects limit parameter."""
    # Create 20 messages
    for i in range(20):
        msg = JobChatMessage(
            job_id=test_job.id,
            role="user" if i % 2 == 0 else "assistant",
            content=f"Message {i}",
        )
        db_session.add(msg)
    await db_session.commit()

    # Retrieve with limit
    history = await get_chat_history(db_session, test_job.id, limit=10)

    assert len(history) == 10


@pytest.mark.asyncio
async def test_chat_messages_per_job(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test that chat messages are isolated per job."""
    # Create second job
    job2 = Job(
        owner_id=test_user.id,
        title="Second Job",
        description="Another job",
        status="running",
    )
    db_session.add(job2)
    await db_session.commit()
    await db_session.refresh(job2)

    # Add messages to each job
    msg1 = JobChatMessage(
        job_id=test_job.id,
        role="user",
        content="Message for job 1",
    )
    msg2 = JobChatMessage(
        job_id=job2.id,
        role="user",
        content="Message for job 2",
    )

    db_session.add_all([msg1, msg2])
    await db_session.commit()

    # Verify isolation
    history1 = await get_chat_history(db_session, test_job.id)
    history2 = await get_chat_history(db_session, job2.id)

    assert len(history1) == 1
    assert len(history2) == 1
    assert history1[0].content == "Message for job 1"
    assert history2[0].content == "Message for job 2"


@pytest.mark.asyncio
async def test_cascade_delete_chat_messages(
    db_session: AsyncSession,
    test_user: User,
):
    """Test that deleting a job deletes its chat messages."""
    # Create job with messages
    job = Job(
        owner_id=test_user.id,
        title="Job with Chat",
        description="Will be deleted",
        status="completed",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Add chat messages
    messages = [
        JobChatMessage(
            job_id=job.id,
            role="user",
            content=f"Message {i}",
        )
        for i in range(3)
    ]

    for msg in messages:
        db_session.add(msg)
    await db_session.commit()

    job_id = job.id

    # Delete job
    await db_session.delete(job)
    await db_session.commit()

    # Verify messages are deleted
    stmt = select(JobChatMessage).where(JobChatMessage.job_id == job_id)
    result = await db_session.execute(stmt)
    remaining_messages = result.scalars().all()

    assert len(remaining_messages) == 0


@pytest.mark.asyncio
async def test_load_job_context_empty_dir(temp_jobs_dir: Path):
    """Test loading context from an empty job directory."""
    job_id = "test_job_123"
    job_dir = temp_jobs_dir / job_id
    job_dir.mkdir()

    context = await load_job_context(job_id, job_dir)

    # Should return empty or minimal context
    assert isinstance(context, str)
    # Empty dir means no context loaded, but function shouldn't crash


@pytest.mark.asyncio
async def test_load_job_context_with_config(temp_jobs_dir: Path):
    """Test loading context with config file."""
    job_id = "test_job_456"
    job_dir = temp_jobs_dir / job_id
    job_dir.mkdir()

    # Create config file
    config = {
        "job_id": job_id,
        "research_question": "What is the crystal structure?",
        "provider": "vertex",
        "model": "claude-3-5-sonnet",
    }

    with open(job_dir / "config.json", "w") as f:
        json.dump(config, f)

    context = await load_job_context(job_id, job_dir)

    assert "What is the crystal structure?" in context
    assert "Research Question" in context


@pytest.mark.asyncio
async def test_load_job_context_with_knowledge_state(temp_jobs_dir: Path):
    """Test loading context with knowledge state including findings."""
    job_id = "test_job_789"
    job_dir = temp_jobs_dir / job_id
    job_dir.mkdir()

    # Create knowledge state
    knowledge_state = {
        "findings": [
            {
                "content": "The protein shows high binding affinity",
                "importance": "high",
                "confidence": "strong",
                "evidence": ["Data point 1", "Data point 2"],
            },
            {
                "content": "Secondary structure is alpha-helical",
                "importance": "medium",
                "confidence": "moderate",
                "evidence": ["Observation 1"],
            },
        ],
        "hypotheses": [
            {
                "hypothesis": "The binding site is at position X",
                "status": "active",
                "rationale": "Based on structural analysis",
            },
        ],
        "literature": [
            {
                "title": "Crystal Structures of Proteins",
                "relevance_score": 0.92,
                "key_findings": ["Finding 1", "Finding 2"],
            },
        ],
        "iteration_summaries": [
            {
                "iteration": 1,
                "strapline": "Initial analysis",
                "summary": "Started with basic structure analysis",
            },
        ],
    }

    with open(job_dir / "knowledge_state.json", "w") as f:
        json.dump(knowledge_state, f)

    context = await load_job_context(job_id, job_dir)

    # Check findings are included
    assert "The protein shows high binding affinity" in context
    assert "high" in context
    assert "Evidence:" in context

    # Check hypotheses
    assert "The binding site is at position X" in context
    assert "active" in context

    # Check literature
    assert "Crystal Structures of Proteins" in context

    # Check iteration summaries
    assert "Initial analysis" in context


@pytest.mark.asyncio
async def test_chat_message_role_validation(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test that chat messages have proper role values."""
    # Create user message
    user_msg = JobChatMessage(
        job_id=test_job.id,
        role="user",
        content="User question",
    )
    db_session.add(user_msg)
    await db_session.commit()

    # Create assistant message
    assistant_msg = JobChatMessage(
        job_id=test_job.id,
        role="assistant",
        content="Assistant response",
    )
    db_session.add(assistant_msg)
    await db_session.commit()

    # Query and verify
    stmt = (
        select(JobChatMessage)
        .where(JobChatMessage.job_id == test_job.id)
        .order_by(JobChatMessage.created_at)
    )
    result = await db_session.execute(stmt)
    messages = result.scalars().all()

    assert messages[0].role == "user"
    assert messages[1].role == "assistant"


@pytest.mark.asyncio
async def test_chat_access_with_rls(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
):
    """Test that chat messages respect RLS policies."""
    # Create job for test_user
    job = Job(
        owner_id=test_user.id,
        title="Private Job",
        description="Test RLS",
        status="running",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    # Add chat message
    message = JobChatMessage(
        job_id=job.id,
        role="user",
        content="Private message",
    )
    db_session.add(message)
    await db_session.commit()

    # Enable RLS before setting user context (superuser bypasses RLS)
    await enable_rls(db_session)

    # Try to access as test_user2 (should fail with RLS)
    await set_current_user(db_session, test_user2.id)

    # Since job is not visible, chat messages won't be either
    stmt = select(JobChatMessage).where(JobChatMessage.job_id == job.id)
    result = await db_session.execute(stmt)
    messages = result.scalars().all()

    # RLS should prevent access (assuming chat messages inherit job access)
    # Actual behavior depends on RLS policy implementation
    assert len(messages) == 0
