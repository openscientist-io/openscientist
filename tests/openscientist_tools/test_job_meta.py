"""Tests for the standalone job-metadata tools."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent
from sqlalchemy import delete, select

from openscientist.database import async_session_local
from openscientist.database.models.job import Job
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.job_meta import (
    save_iteration_summary,
    set_consensus_answer,
    set_job_title,
    set_status,
)
from openscientist_tools.state import STATE


@asynccontextmanager
async def _spawned_for_job(
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    tmp_path: Path,
    test_database_url: str,
    job_id: UUID,
) -> AsyncGenerator[ClientSession, None]:
    """Create a Job row, spawn the subprocess MCP server with env bound to
    that job_id, yield a connected `ClientSession`, then delete the Job."""
    async with async_session_local() as setup:
        setup.add(
            Job(
                id=job_id,
                research_question="job_meta subprocess test",
                llm_provider="mock",
                llm_config={"model": "mock-model-v1"},
                status="pending",
            )
        )
        await setup.commit()
    try:
        env = server_env(tmp_path, OPENSCIENTIST_JOB_ID=str(job_id))
        env["DATABASE_URL"] = test_database_url
        env["OPENSCIENTIST_SECRET_KEY"] = os.environ["OPENSCIENTIST_SECRET_KEY"]
        async with stdio_client(server_params(env)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        async with async_session_local() as cleanup:
            await cleanup.execute(delete(Job).where(Job.id == job_id))
            await cleanup.commit()


def _text(result: object) -> str:
    blocks = result.content  # type: ignore[attr-defined]
    (block,) = blocks
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture(autouse=True)
def _state_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(STATE, "job_id", "test-job-uuid")


async def test_set_status_updates_ks_and_returns_message(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = await set_status("Running PCA on expression data")
    assert result == "✅ Status updated: Running PCA on expression data"
    assert patched_ks_persistence.data["agent_status"] == "Running PCA on expression data"


async def test_set_status_truncates_at_80_chars(
    patched_ks_persistence: KnowledgeState,
) -> None:
    long_message = "x" * 150
    result = await set_status(long_message)
    trimmed = "x" * 80
    assert result == f"✅ Status updated: {trimmed}"
    assert patched_ks_persistence.data["agent_status"] == trimmed


async def test_save_iteration_summary_appends(
    patched_ks_persistence: KnowledgeState,
) -> None:
    patched_ks_persistence.data["iteration"] = 3
    result = await save_iteration_summary("Found elevated carnosine in cold-adapted cells")
    assert result.startswith("✅ Iteration summary saved: Found elevated carnosine")
    summaries = patched_ks_persistence.data["iteration_summaries"]
    assert len(summaries) == 1
    assert summaries[0]["iteration"] == 3
    assert summaries[0]["summary"] == "Found elevated carnosine in cold-adapted cells"


async def test_save_iteration_summary_with_strapline(
    patched_ks_persistence: KnowledgeState,
) -> None:
    patched_ks_persistence.data["iteration"] = 1
    await save_iteration_summary(
        "Detailed body of work", strapline="Cold adapted cells produce carnosine"
    )
    summaries = patched_ks_persistence.data["iteration_summaries"]
    assert summaries[0]["strapline"] == "Cold adapted cells produce carnosine"


async def test_set_consensus_answer_strips_and_stores(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = await set_consensus_answer("   The answer is carnosine.   ")
    assert result == "✅ Consensus answer set"
    assert patched_ks_persistence.data["consensus_answer"] == "The answer is carnosine."


async def test_set_job_title_too_short() -> None:
    result = await set_job_title("ab")
    assert result.startswith("❌ Title too short")


async def test_set_job_title_too_long() -> None:
    """A title one character over the 100-char maximum is rejected."""
    result = await set_job_title("x" * 101)
    assert result.startswith("❌")


async def test_set_job_title_invalid_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-UUID job id is rejected and the offending id is reported."""
    monkeypatch.setattr(STATE, "job_id", "not-a-uuid")
    result = await set_job_title("Valid Title")
    assert result.startswith("❌")
    assert "not-a-uuid" in result


async def test_set_job_title_min_boundary(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    """Title at the inclusive minimum (3 chars) passes validation."""
    monkeypatch.setattr(STATE, "job_id", str(uuid4()))
    result = await set_job_title("abc")
    assert "too short" not in result.lower()
    assert "too long" not in result.lower()


async def test_set_job_title_max_boundary(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    """Title at the inclusive maximum (100 chars) passes validation."""
    monkeypatch.setattr(STATE, "job_id", str(uuid4()))
    result = await set_job_title("x" * 100)
    assert "too long" not in result.lower()


async def test_set_job_title_job_not_found(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    nonexistent_id = uuid4()
    monkeypatch.setattr(STATE, "job_id", str(nonexistent_id))
    result = await set_job_title("Nope")
    # A valid UUID with no matching job is rejected, not silently dropped
    assert result.startswith("❌")


# ----- Full-call subprocess + real-DB tests, one per tool -----


async def test_set_status_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    async with _spawned_for_job(
        server_env, server_params, tmp_path, test_database_url, job_id
    ) as mcp:
        response = await mcp.call_tool("set_status", {"message": "Running subprocess test"})
        assert _text(response) == "✅ Status updated: Running subprocess test"

        async with async_session_local() as session:
            row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
            assert row.agent_status == "Running subprocess test"


async def test_set_job_title_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    async with _spawned_for_job(
        server_env, server_params, tmp_path, test_database_url, job_id
    ) as mcp:
        response = await mcp.call_tool("set_job_title", {"title": "Subprocess Title"})
        assert _text(response) == "✅ Job title set: Subprocess Title"

        async with async_session_local() as session:
            row = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
            assert row.short_title == "Subprocess Title"


async def test_save_iteration_summary_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    async with _spawned_for_job(
        server_env, server_params, tmp_path, test_database_url, job_id
    ) as mcp:
        response = await mcp.call_tool(
            "save_iteration_summary",
            {"summary": "Found elevated carnosine", "strapline": "Cold cells make it"},
        )
        assert _text(response).startswith("✅ Iteration summary saved: Found elevated carnosine")

        reloaded = await KnowledgeState.load_from_database(str(job_id))
        assert len(reloaded.data["iteration_summaries"]) == 1
        entry = reloaded.data["iteration_summaries"][0]
        assert entry["summary"] == "Found elevated carnosine"
        assert entry["strapline"] == "Cold cells make it"


async def test_set_consensus_answer_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    async with _spawned_for_job(
        server_env, server_params, tmp_path, test_database_url, job_id
    ) as mcp:
        response = await mcp.call_tool(
            "set_consensus_answer", {"answer": "  The answer is carnosine.  "}
        )
        assert _text(response) == "✅ Consensus answer set"

        reloaded = await KnowledgeState.load_from_database(str(job_id))
        assert reloaded.data["consensus_answer"] == "The answer is carnosine."
