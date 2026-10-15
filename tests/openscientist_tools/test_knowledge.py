"""Tests for the standalone knowledge tools."""

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
from sqlalchemy import delete

from openscientist.database import AsyncSessionLocal
from openscientist.database.models.job import Job
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.knowledge import update_knowledge_state
from openscientist_tools.state import STATE


@asynccontextmanager
async def _spawned_for_job(
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    tmp_path: Path,
    test_database_url: str,
    job_id: UUID,
    *,
    env_overrides: dict[str, str] | None = None,
) -> AsyncGenerator[ClientSession, None]:
    """Create a Job row, spawn the subprocess MCP server with env bound to
    that job_id (and optional ``env_overrides``), yield a connected
    `ClientSession`, then delete the Job."""
    async with AsyncSessionLocal(thread_safe=True) as setup:
        setup.add(
            Job(
                id=job_id,
                research_question="knowledge subprocess test",
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
        if env_overrides:
            env.update(env_overrides)
        async with stdio_client(server_params(env)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        async with AsyncSessionLocal(thread_safe=True) as cleanup:
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


# ----- In-process branch coverage (mocked KS persistence) -----


def test_update_knowledge_state_records_finding(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = update_knowledge_state(
        title="Carnosine accumulation in cold-adapted cells",
        evidence="p<0.01, n=12",
        interpretation="suggests upregulated synthesis",
        description="primary discovery",
    )
    assert result.startswith("✅ Finding #1 recorded: Carnosine accumulation in cold-adapted cells")
    findings = patched_ks_persistence.data["findings"]
    assert len(findings) == 1
    assert findings[0]["biological_interpretation"] == "suggests upregulated synthesis"
    last_log = patched_ks_persistence.data["analysis_log"][-1]
    assert last_log["action"] == "update_knowledge_state"
    assert last_log["title"] == "Carnosine accumulation in cold-adapted cells"


def test_update_knowledge_state_with_unchecked_citation(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = update_knowledge_state(
        title="Finding with citation",
        evidence="p=0.02",
        citations=[
            {
                "pmid": "99999",
                "snippet": "anything",
                "explanation": "unrelated",
            }
        ],
    )
    assert "ℹ️" in result
    assert "PMID:99999" in result
    assert "paper not in literature list" in result


def test_update_knowledge_state_with_mismatch_citation(
    patched_ks_persistence: KnowledgeState,
) -> None:
    patched_ks_persistence.add_literature(
        pmid="12345",
        title="Real paper",
        abstract="Cells adapt to cold by upregulating chaperones.",
        search_query="cold adaptation",
    )
    result = update_knowledge_state(
        title="Finding with mismatch",
        evidence="p=0.04",
        citations=[
            {
                "pmid": "12345",
                "snippet": "totally unrelated nonsense phrase that is absent",
                "explanation": "wrong snippet",
            }
        ],
    )
    assert "⚠️" in result
    assert "PMID:12345" in result
    assert "snippet not found in abstract" in result


# ----- Full-call subprocess + real-DB tests, one per tool -----


async def test_update_knowledge_state_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    async with _spawned_for_job(
        server_env,
        server_params,
        tmp_path,
        test_database_url,
        job_id,
    ) as mcp:
        response = await mcp.call_tool(
            "update_knowledge_state",
            {
                "title": "Subprocess finding",
                "evidence": "p<0.05",
                "interpretation": "real test",
                "description": "verifying subprocess",
            },
        )
        assert _text(response).startswith("✅ Finding #1 recorded: Subprocess finding")

        reloaded = KnowledgeState.load_from_database_sync(str(job_id))
        assert len(reloaded.data["findings"]) == 1
        assert reloaded.data["findings"][0]["title"] == "Subprocess finding"


async def test_hypothesis_tools_are_not_registered(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    """The hypothesis-tracking opt-in was removed: the server never exposes
    add_hypothesis/update_hypothesis, even if a stale env var asks for it."""
    job_id = uuid4()
    async with _spawned_for_job(
        server_env,
        server_params,
        tmp_path,
        test_database_url,
        job_id,
        env_overrides={"OPENSCIENTIST_USE_HYPOTHESES": "1"},
    ) as mcp:
        tools = await mcp.list_tools()
        names = {t.name for t in tools.tools}
        assert "update_knowledge_state" in names
        assert "add_hypothesis" not in names
        assert "update_hypothesis" not in names
