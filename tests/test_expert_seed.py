"""Tests for the Alembic data migration that seeds vendored experts."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.agent.expert_loader import load_enabled_experts
from openscientist.agent.expert_seed import all_seed_rows, run_seed
from openscientist.database.models import Expert

EXPECTED_SLUGS = {
    "research-lead",
    "research-subagent",
    "citations-agent",
    "data-scientist",
    "python-pro",
    "scientific-literature-researcher",
    "data-researcher",
    "research-analyst",
}


@pytest.mark.asyncio
async def test_seed_migration_inserted_eight_rows(db_session: AsyncSession) -> None:
    """After `alembic upgrade head`, 8 vendored experts exist."""
    result = await db_session.execute(select(Expert).where(Expert.source != "openscientist"))
    rows = list(result.scalars())
    assert len(rows) == 8


@pytest.mark.asyncio
async def test_seed_migration_slugs_match_contract(db_session: AsyncSession) -> None:
    """Seeded slugs must match the contract used by the seed helper."""
    result = await db_session.execute(select(Expert.slug).where(Expert.source != "openscientist"))
    slugs = {row for row in result.scalars()}
    assert slugs == EXPECTED_SLUGS


@pytest.mark.asyncio
async def test_seed_migration_attribution_fields_populated(
    db_session: AsyncSession,
) -> None:
    """Every seeded row carries source and source_url."""
    result = await db_session.execute(select(Expert).where(Expert.source != "openscientist"))
    for row in result.scalars():
        assert row.source in {"anthropic", "wshobson", "voltagent"}
        assert row.source_url, f"Missing source_url for {row.slug}"


@pytest.mark.asyncio
async def test_seed_rows_loadable_by_expert_loader(db_session: AsyncSession) -> None:
    """After seeding, load_enabled_experts returns all 8 AgentDefinition rows."""
    experts = await load_enabled_experts(db_session)
    seeded = {slug for slug in experts if slug in EXPECTED_SLUGS}
    assert seeded == EXPECTED_SLUGS


@pytest.mark.asyncio
async def test_seed_rows_have_non_null_prompt_and_description(
    db_session: AsyncSession,
) -> None:
    """Prompt and description columns must be non-empty after seed."""
    result = await db_session.execute(select(Expert).where(Expert.source != "openscientist"))
    for row in result.scalars():
        assert row.prompt and len(row.prompt.strip()) > 100
        assert row.description and len(row.description.strip()) > 10


@pytest.mark.asyncio
async def test_seed_is_idempotent_on_rerun(db_session: AsyncSession) -> None:
    """Running run_seed() a second time must not create duplicates."""
    before_result = await db_session.execute(select(Expert).where(Expert.source != "openscientist"))
    before_count = len(list(before_result.scalars()))
    assert before_count == 8

    await run_seed(db_session)
    await db_session.commit()

    after_result = await db_session.execute(select(Expert).where(Expert.source != "openscientist"))
    after_count = len(list(after_result.scalars()))
    assert after_count == 8


@pytest.mark.asyncio
async def test_seed_rows_match_helper_contract(db_session: AsyncSession) -> None:
    """Seeded rows have the same slugs as all_seed_rows() returns."""
    helper_slugs = {r.slug for r in all_seed_rows()}
    assert helper_slugs == EXPECTED_SLUGS
