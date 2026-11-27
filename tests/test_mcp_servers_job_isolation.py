"""Registering an MCP server leaves every harness's job workspace and MCP
configuration as it was: a registration is catalog data that no job reads."""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.admin.mcp_servers import (
    McpServerRegistration,
    list_mcp_servers,
    parse_registration,
    register_mcp_server,
)
from openscientist.agent.base import AgentConfig
from openscientist.agent.claude_code_agent import ClaudeCodeAgent
from openscientist.agent.codex_agent import CodexAgent
from openscientist.agent.omp_agent import OmpAgent
from openscientist.database.models import McpServer, User
from openscientist.database.session import get_admin_session
from tests.db_outage import Forwarder
from tests.helpers import StubClaudeProvider, StubCodexProvider

_SLUG = "catalog-only-probe"
_HOST = "catalog-only-probe.example.org"


@pytest_asyncio.fixture
async def admin_id(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[UUID]:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        await db.execute(delete(McpServer))
        admin = User(email=f"admin-{uuid4().hex}@mcp.test", name="Admin", is_approved=True)
        db.add(admin)
        await db.commit()
    yield admin.id
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(McpServer))
        await db.execute(delete(User).where(User.id == admin.id))
        await db.commit()


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


async def _prepare_jobs(jobs_dir: Path) -> dict[str, Any]:
    """Prepare one job workspace per harness, as a discovery run does, and
    return each workspace's files and Claude's MCP server configuration."""
    shutil.rmtree(jobs_dir, ignore_errors=True)
    claude = ClaudeCodeAgent(
        AgentConfig(job_dir=jobs_dir / "claude", system_prompt="prompt"), StubClaudeProvider()
    )
    codex = CodexAgent(
        AgentConfig(job_dir=jobs_dir / "codex", system_prompt="prompt"), StubCodexProvider()
    )
    omp = OmpAgent(
        AgentConfig(job_dir=jobs_dir / "omp", system_prompt="prompt"), StubClaudeProvider()
    )
    for agent in (claude, codex, omp):
        agent._config.job_dir.mkdir(parents=True)
        await agent.prepare_job_workspace(use_hypotheses=True)
    codex._write_codex_config()
    return {
        "claude_mcp_servers": claude._build_options().mcp_servers,
        "files": _files(jobs_dir),
    }


@pytest.mark.asyncio
async def test_a_registration_changes_no_job_preparation_or_harness_mcp_configuration(
    admin_id: UUID, tmp_path: Path
) -> None:
    jobs_dir = tmp_path / "jobs"
    before = await _prepare_jobs(jobs_dir)
    assert set(before["claude_mcp_servers"]) == {"openscientist-tools"}
    assert any(name.endswith("config.toml") for name in before["files"])
    assert any(name.endswith("mcp.json") for name in before["files"])

    registration = parse_registration(_SLUG, "Catalog only probe", f"https://{_HOST}/mcp")
    assert isinstance(registration, McpServerRegistration)
    async with get_admin_session() as session:
        assert (await register_mcp_server(session, registration, admin_id))[0]
    async with get_admin_session() as session:
        assert [server.slug for server in await list_mcp_servers(session)] == [_SLUG]

    after = await _prepare_jobs(jobs_dir)

    assert after == before
    for content in after["files"].values():
        assert _SLUG.encode() not in content
        assert _HOST.encode() not in content
