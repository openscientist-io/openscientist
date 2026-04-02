"""
Tests for job chat functionality.

Tests chat message creation, conversation history, context loading,
and executor error handling.
"""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.agent.protocol import IterationResult
from openscientist.database.models import Job, JobChatMessage, User
from openscientist.database.rls import set_current_user
from openscientist.job_chat import get_chat_history, load_job_context, send_chat_message
from tests.helpers import enable_rls


@pytest.mark.asyncio
async def test_create_chat_message(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test creating a chat message."""
    _ = test_user
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
    _ = test_user
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
    _ = test_user
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
    _ = test_user
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
async def test_load_job_context_empty_dir():
    """Test loading context when no knowledge state exists in the DB."""
    job_id = "not-a-valid-uuid"

    context = await load_job_context(job_id)

    # Should return empty or minimal context
    assert isinstance(context, str)
    # Missing DB record means no context loaded, but function shouldn't crash


@pytest.mark.asyncio
async def test_load_job_context_with_knowledge_state_config():
    """Test loading context with research question in knowledge state."""
    from openscientist.knowledge_state import KnowledgeState

    job_id = "test_job_456"

    ks = KnowledgeState(job_id, "What is the crystal structure?", 10)

    with patch(
        "openscientist.job_chat.KnowledgeState.load_from_database_sync",
        return_value=ks,
    ):
        context = await load_job_context(job_id)

    assert "What is the crystal structure?" in context
    assert "Research Question" in context


@pytest.mark.asyncio
async def test_load_job_context_with_knowledge_state():
    """Test loading context with knowledge state including findings."""
    from openscientist.knowledge_state import KnowledgeState

    job_id = "test_job_789"

    ks = KnowledgeState(job_id, "Protein analysis?", 10)
    ks.data["findings"] = [
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
    ]
    ks.data["hypotheses"] = [
        {
            "hypothesis": "The binding site is at position X",
            "status": "active",
            "rationale": "Based on structural analysis",
        },
    ]
    ks.data["literature"] = [
        {
            "title": "Crystal Structures of Proteins",
            "relevance_score": 0.92,
            "key_findings": ["Finding 1", "Finding 2"],
        },
    ]
    ks.data["iteration_summaries"] = [
        {
            "iteration": 1,
            "strapline": "Initial analysis",
            "summary": "Started with basic structure analysis",
        },
    ]

    with patch(
        "openscientist.job_chat.KnowledgeState.load_from_database_sync",
        return_value=ks,
    ):
        context = await load_job_context(job_id)

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
async def test_load_job_context_supports_modern_knowledge_state_keys():
    """Context rendering should support modern finding/hypothesis key names."""
    from openscientist.knowledge_state import KnowledgeState

    job_id = "test_job_modern_keys"

    ks = KnowledgeState(job_id, "Q?", 10)
    ks.data["findings"] = [
        {
            "title": "Modern finding title",
            "importance": "high",
            "confidence": "strong",
        }
    ]
    ks.data["hypotheses"] = [
        {
            "statement": "Modern hypothesis statement",
            "status": "supported",
        }
    ]

    with patch(
        "openscientist.job_chat.KnowledgeState.load_from_database_sync",
        return_value=ks,
    ):
        context = await load_job_context(job_id)
    assert "Modern finding title" in context
    assert "Modern hypothesis statement" in context


@pytest.mark.asyncio
async def test_chat_message_role_validation(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
):
    """Test that chat messages have proper role values."""
    _ = test_user
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


@pytest.mark.asyncio
async def test_send_chat_message_success(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
    temp_jobs_dir: Path,
):
    """Test that send_chat_message stores messages and returns response."""
    _ = test_user
    job_dir = temp_jobs_dir / str(test_job.id)
    job_dir.mkdir()

    mock_executor = AsyncMock()
    mock_executor.run_iteration.return_value = IterationResult(
        success=True,
        output="The main findings indicate...",
        tool_calls=0,
        transcript=[],
    )

    with (
        patch("openscientist.agent.sdk_executor.SDKAgentExecutor", return_value=mock_executor),
        patch("openscientist.providers.get_provider") as mock_get_provider,
    ):
        mock_get_provider.return_value.setup_environment.return_value = None

        response = await send_chat_message(
            db_session, test_job.id, "What are the main findings?", job_dir
        )

    assert response == "The main findings indicate..."
    chat_claude_md = job_dir / ".claude" / "CLAUDE.md"
    assert chat_claude_md.exists()
    assert "OpenScientist Job Chat Assistant" in chat_claude_md.read_text(encoding="utf-8")

    # Verify both messages were stored
    history = await get_chat_history(db_session, test_job.id)
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[0].content == "What are the main findings?"
    assert history[1].role == "assistant"
    assert history[1].content == "The main findings indicate..."


@pytest.mark.asyncio
async def test_send_chat_message_raises_on_executor_failure(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
    temp_jobs_dir: Path,
):
    """Test that executor failure raises RuntimeError instead of returning empty string."""
    _ = test_user
    job_dir = temp_jobs_dir / str(test_job.id)
    job_dir.mkdir()

    mock_executor = AsyncMock()
    mock_executor.run_iteration.return_value = IterationResult(
        success=False,
        output="",
        tool_calls=0,
        transcript=[],
        error="Process exited with code 1",
    )

    with (
        patch("openscientist.agent.sdk_executor.SDKAgentExecutor", return_value=mock_executor),
        patch("openscientist.providers.get_provider") as mock_get_provider,
    ):
        mock_get_provider.return_value.setup_environment.return_value = None

        with pytest.raises(RuntimeError, match="Process exited with code 1"):
            await send_chat_message(db_session, test_job.id, "What are the main findings?", job_dir)

    # Verify no messages were stored (commit never reached)
    history = await get_chat_history(db_session, test_job.id)
    assert len(history) == 0


@pytest.mark.asyncio
async def test_send_chat_message_raises_generic_on_empty_error(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
    temp_jobs_dir: Path,
):
    """Test that executor failure with no error message still raises."""
    _ = test_user
    job_dir = temp_jobs_dir / str(test_job.id)
    job_dir.mkdir()

    mock_executor = AsyncMock()
    mock_executor.run_iteration.return_value = IterationResult(
        success=False,
        output="",
        tool_calls=0,
        transcript=[],
    )

    with (
        patch("openscientist.agent.sdk_executor.SDKAgentExecutor", return_value=mock_executor),
        patch("openscientist.providers.get_provider") as mock_get_provider,
    ):
        mock_get_provider.return_value.setup_environment.return_value = None

        with pytest.raises(RuntimeError, match="Chat executor returned no output"):
            await send_chat_message(db_session, test_job.id, "Hello", job_dir)


@pytest.mark.asyncio
async def test_system_prompt_does_not_include_job_context(
    db_session: AsyncSession,
    test_user: User,
    test_job: Job,
    temp_jobs_dir: Path,
):
    """Test that the system prompt is small and doesn't embed job context."""
    _ = test_user
    job_dir = temp_jobs_dir / str(test_job.id)
    job_dir.mkdir()

    from openscientist.knowledge_state import KnowledgeState

    # Create a large knowledge state that would blow up ARG_MAX if embedded
    large_ks = KnowledgeState(str(test_job.id), "Q?", 10)
    large_ks.data["findings"] = [{"content": "x" * 50000}]

    captured_system_prompt = None

    class FakeExecutor:
        def __init__(self, *, job_dir, data_file, system_prompt):
            _ = (job_dir, data_file)
            nonlocal captured_system_prompt
            captured_system_prompt = system_prompt

        async def run_iteration(self, prompt, *, reset_session=False):
            _ = (prompt, reset_session)
            return IterationResult(
                success=True,
                output="Response",
                tool_calls=0,
                transcript=[],
            )

        async def shutdown(self):
            pass

    with (
        patch("openscientist.agent.sdk_executor.SDKAgentExecutor", FakeExecutor),
        patch("openscientist.providers.get_provider") as mock_get_provider,
        patch(
            "openscientist.job_chat.KnowledgeState.load_from_database_sync",
            return_value=large_ks,
        ),
    ):
        mock_get_provider.return_value.setup_environment.return_value = None

        await send_chat_message(db_session, test_job.id, "Summarize findings", job_dir)

    # System prompt should be small — just instructions, not embedded context
    assert captured_system_prompt is not None
    assert len(captured_system_prompt) < 2000
    # Should not reference deprecated knowledge_state.json file
    assert "knowledge_state.json" not in captured_system_prompt
    # Should NOT contain the large content
    assert "x" * 1000 not in captured_system_prompt
