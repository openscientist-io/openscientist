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

from openscientist.database import async_session_local
from openscientist.database.models.job import Job
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.knowledge import (
    add_hypothesis,
    update_hypothesis,
    update_knowledge_state,
)
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
    async with async_session_local() as setup:
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


# ----- In-process branch coverage (mocked KS persistence) -----


async def test_update_knowledge_state_records_finding(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = await update_knowledge_state(
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


async def test_update_knowledge_state_with_unchecked_citation(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = await update_knowledge_state(
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


async def test_update_knowledge_state_with_mismatch_citation(
    patched_ks_persistence: KnowledgeState,
) -> None:
    patched_ks_persistence.add_literature(
        pmid="12345",
        title="Real paper",
        abstract="Cells adapt to cold by upregulating chaperones.",
        search_query="cold adaptation",
    )
    result = await update_knowledge_state(
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


async def test_add_hypothesis_returns_id_and_records(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = await add_hypothesis("Cold exposure increases carnosine via X pathway")
    assert result == "✅ Hypothesis H001 added: Cold exposure increases carnosine via X pathway"
    hyps = patched_ks_persistence.data["hypotheses"]
    assert len(hyps) == 1
    assert hyps[0]["id"] == "H001"
    assert hyps[0]["statement"] == "Cold exposure increases carnosine via X pathway"


async def test_update_hypothesis_invalid_status(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = await update_hypothesis(hypothesis_id="H001", status="bogus")
    assert result.startswith("❌ Invalid status 'bogus'")
    assert "pending, testing, supported, rejected" in result


async def test_update_hypothesis_nonexistent_id_returns_error(
    patched_ks_persistence: KnowledgeState,
) -> None:
    result = await update_hypothesis(hypothesis_id="H999", status="supported")
    assert result.startswith("❌ ")


async def test_update_hypothesis_supported_includes_emoji_and_result(
    patched_ks_persistence: KnowledgeState,
) -> None:
    patched_ks_persistence.add_hypothesis(statement="Stub", proposed_by="agent")
    patched_ks_persistence.data["iteration"] = 4

    result = await update_hypothesis(
        hypothesis_id="H001",
        status="supported",
        result_summary="strong correlation",
        p_value="p=0.001",
        effect_size="r=0.7",
        conclusion="primary mechanism likely",
    )
    assert result == "✅ Hypothesis H001 updated to 'supported'"
    entry = next(h for h in patched_ks_persistence.data["hypotheses"] if h["id"] == "H001")
    assert entry["status"] == "supported"
    assert entry["tested_at_iteration"] == 4
    assert entry["result"]["summary"] == "strong correlation"
    assert entry["result"]["p_value"] == "p=0.001"


@pytest.mark.parametrize(
    "status,emoji",
    [("testing", "🔬"), ("supported", "✅"), ("rejected", "❌")],
)
async def test_update_hypothesis_status_emoji_map(
    patched_ks_persistence: KnowledgeState,
    status: str,
    emoji: str,
) -> None:
    patched_ks_persistence.add_hypothesis(statement="Stub", proposed_by="agent")
    result = await update_hypothesis(hypothesis_id="H001", status=status)
    assert result == f"{emoji} Hypothesis H001 updated to '{status}'"


# ----- Full-call subprocess + real-DB tests, one per tool -----


async def test_update_knowledge_state_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    """update_knowledge_state is unconditional, so this runs WITHOUT
    OPENSCIENTIST_USE_HYPOTHESES set, matching the default production env."""
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

        reloaded = await KnowledgeState.load_from_database(str(job_id))
        assert len(reloaded.data["findings"]) == 1
        assert reloaded.data["findings"][0]["title"] == "Subprocess finding"


async def test_add_hypothesis_via_subprocess(
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
        env_overrides={"OPENSCIENTIST_USE_HYPOTHESES": "1"},
    ) as mcp:
        response = await mcp.call_tool("add_hypothesis", {"statement": "Cold induces carnosine"})
        assert _text(response) == "✅ Hypothesis H001 added: Cold induces carnosine"

        reloaded = await KnowledgeState.load_from_database(str(job_id))
        assert len(reloaded.data["hypotheses"]) == 1
        assert reloaded.data["hypotheses"][0]["statement"] == "Cold induces carnosine"


async def test_update_hypothesis_via_subprocess(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
    test_database_url: str,
    _apply_migrations_once: None,
) -> None:
    job_id = uuid4()
    async with async_session_local() as setup:
        setup.add(
            Job(
                id=job_id,
                research_question="seeded hypothesis",
                llm_provider="mock",
                llm_config={"model": "mock-model-v1"},
                status="pending",
            )
        )
        await setup.commit()
    try:
        seeded = await KnowledgeState.load_from_database(str(job_id))
        seeded.add_hypothesis(statement="Seed statement", proposed_by="agent")
        await seeded.save_to_database(str(job_id))

        env = server_env(tmp_path, OPENSCIENTIST_JOB_ID=str(job_id))
        env["DATABASE_URL"] = test_database_url
        env["OPENSCIENTIST_SECRET_KEY"] = os.environ["OPENSCIENTIST_SECRET_KEY"]
        env["OPENSCIENTIST_USE_HYPOTHESES"] = "1"

        async with stdio_client(server_params(env)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                response = await session.call_tool(
                    "update_hypothesis",
                    {"hypothesis_id": "H001", "status": "supported"},
                )
                assert _text(response) == "✅ Hypothesis H001 updated to 'supported'"

        reloaded = await KnowledgeState.load_from_database(str(job_id))
        entry = next(h for h in reloaded.data["hypotheses"] if h["id"] == "H001")
        assert entry["status"] == "supported"
    finally:
        async with async_session_local() as cleanup:
            await cleanup.execute(delete(Job).where(Job.id == job_id))
            await cleanup.commit()


# ----- Conditional registration tests -----


async def test_hypothesis_tools_present_when_use_hypotheses_true(
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
        env_overrides={"OPENSCIENTIST_USE_HYPOTHESES": "1"},
    ) as mcp:
        tools = await mcp.list_tools()
        names = {t.name for t in tools.tools}
        assert "update_knowledge_state" in names
        assert "add_hypothesis" in names
        assert "update_hypothesis" in names


async def test_hypothesis_tools_absent_when_use_hypotheses_false(
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
        env_overrides={"OPENSCIENTIST_USE_HYPOTHESES": "0"},
    ) as mcp:
        tools = await mcp.list_tools()
        names = {t.name for t in tools.tools}
        assert "update_knowledge_state" in names
        assert "add_hypothesis" not in names
        assert "update_hypothesis" not in names
