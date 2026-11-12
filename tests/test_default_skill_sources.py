"""Default skill source seeding preserves operator configuration."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist import web_app
from openscientist.database import session as database_session
from openscientist.database.models import Skill, SkillSource
from openscientist.skill_ingestion import GitHubSkillIngester
from tests.test_skill_ingestion import build_repo_tarball

DEFAULT_NAME = "Claude Scientific Skills"
DEFAULT_URL = "https://github.com/K-Dense-AI/scientific-agent-skills"
RENAMED_URL = "https://github.com/K-Dense-AI/claude-scientific-skills"
RECORDED_SHA = "a" * 40


@pytest.fixture
def seed_session(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> AsyncSession:
    @asynccontextmanager
    async def admin_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(database_session, "get_admin_session", admin_session)
    return db_session


async def test_fresh_default_source_uses_skills_directory(seed_session: AsyncSession) -> None:
    await web_app._ensure_default_skill_sources()

    source = (
        await seed_session.execute(select(SkillSource).where(SkillSource.name == DEFAULT_NAME))
    ).scalar_one()
    assert source.source_type == "github"
    assert source.url == DEFAULT_URL
    assert source.branch == "main"
    assert source.skills_path == "skills"
    assert source.is_enabled is True
    assert source.last_commit_sha is None


@pytest.mark.parametrize("is_enabled", [True, False], ids=["enabled", "disabled"])
@pytest.mark.parametrize(
    "seeded_url", [DEFAULT_URL, RENAMED_URL], ids=["current-url", "renamed-url"]
)
async def test_legacy_default_source_repairs_path_and_clears_sha(
    seed_session: AsyncSession, is_enabled: bool, seeded_url: str, sample_skill_markdown: str
) -> None:
    source = SkillSource(
        name=DEFAULT_NAME,
        source_type="github",
        url=seeded_url,
        branch="main",
        skills_path="scientific-skills",
        is_enabled=is_enabled,
        last_commit_sha=RECORDED_SHA,
    )
    seed_session.add(source)
    await seed_session.commit()
    source_id = source.id

    await web_app._ensure_default_skill_sources()
    await seed_session.refresh(source)

    assert source.id == source_id
    assert source.skills_path == "skills"
    assert source.last_commit_sha is None
    assert source.is_enabled is is_enabled
    assert source.source_type == "github"
    assert source.url == DEFAULT_URL
    assert source.branch == "main"
    assert (
        await seed_session.execute(select(SkillSource.id).where(SkillSource.name == DEFAULT_NAME))
    ).scalars().all() == [source_id]

    archive = build_repo_tarball({"skills/test/SKILL.md": sample_skill_markdown})

    def github_response(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": RECORDED_SHA})
        if request.url.path.endswith("/tarball/main"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    ingester = GitHubSkillIngester()
    async with httpx.AsyncClient(transport=httpx.MockTransport(github_response)) as client:
        ingester._client = client
        stats = await ingester.sync_source(seed_session, source)

    assert stats["created"] == 1
    skill = (
        await seed_session.execute(select(Skill).where(Skill.source_id == source.id))
    ).scalar_one()
    assert skill.source_path == "skills/test/SKILL.md"
    assert skill.commit_sha == RECORDED_SHA


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("name", "Operator Scientific Skills", id="custom-name"),
        pytest.param("url", "https://github.com/operator/skills", id="custom-url"),
        pytest.param("branch", "operator-branch", id="custom-branch"),
        pytest.param("source_type", "local", id="custom-source-type"),
        pytest.param("skills_path", "operator-skills", id="custom-path"),
        pytest.param("skills_path", "", id="repository-root"),
        pytest.param("skills_path", "skills", id="already-correct"),
    ],
)
async def test_existing_source_configuration_and_sync_state_are_preserved(
    seed_session: AsyncSession, field: str, value: str
) -> None:
    configuration = {
        "name": DEFAULT_NAME,
        "source_type": "github",
        "url": DEFAULT_URL,
        "branch": "main",
        "skills_path": "scientific-skills",
        "path": "/operator/skills",
    }
    configuration[field] = value
    synced_at = datetime(2026, 1, 1, tzinfo=UTC)
    source = SkillSource(
        **configuration,
        is_enabled=False,
        last_commit_sha=RECORDED_SHA,
        last_synced_at=synced_at,
        sync_error="Operator sync error",
    )
    seed_session.add(source)
    await seed_session.commit()
    source_id = source.id

    await web_app._ensure_default_skill_sources()
    await seed_session.refresh(source)

    assert source.id == source_id
    for name, expected in configuration.items():
        assert getattr(source, name) == expected
    assert source.is_enabled is False
    assert source.last_commit_sha == RECORDED_SHA
    assert source.last_synced_at == synced_at
    assert source.sync_error == "Operator sync error"
