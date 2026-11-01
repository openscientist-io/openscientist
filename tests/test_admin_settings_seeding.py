"""Seeding writes the environment's values once, and leaves an edited row alone."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.admin_settings import seed_admin_settings
from openscientist.settings import clear_settings_cache


@pytest.fixture
def seeded_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """The environment seeding reads, with inf limits and Docker memory strings."""
    monkeypatch.setenv("MAX_JOB_COST_USD", "12.5")
    monkeypatch.setenv("MAX_PROJECT_SPEND_TOTAL_USD", "inf")
    monkeypatch.setenv("MAX_PROJECT_SPEND_24H_USD", "inf")
    monkeypatch.setenv("WARN_PROJECT_SPEND_24H_USD", "inf")
    monkeypatch.setenv("OPENSCIENTIST_AGENT_MEMORY", "8g")
    monkeypatch.setenv("OPENSCIENTIST_EXECUTOR_MEMORY", "512m")
    monkeypatch.setenv("OPENSCIENTIST_AIRGAPPED", "true")
    clear_settings_cache()
    yield
    clear_settings_cache()


async def _clear_row(test_engine: AsyncEngine) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM admin_settings"))


async def _row(test_engine: AsyncEngine) -> dict[str, object]:
    async with test_engine.connect() as conn:
        row = (await conn.execute(text("SELECT * FROM admin_settings"))).mappings().first()
    if row is None:
        raise AssertionError("the admin settings row is missing")
    return dict(row)


@pytest.mark.asyncio
async def test_seeding_writes_null_budgets_and_mib(
    web_login: None, seeded_env: None, test_engine: AsyncEngine
) -> None:
    await _clear_row(test_engine)
    try:
        await seed_admin_settings()
        row = await _row(test_engine)
        assert row["max_job_cost_usd"] == 12.5
        assert row["max_project_spend_total_usd"] is None
        assert row["max_project_spend_24h_usd"] is None
        assert row["warn_project_spend_24h_usd"] is None
        assert row["agent_memory_mib"] == 8192
        assert row["executor_memory_mib"] == 512
        assert row["airgapped"] is True
    finally:
        await _clear_row(test_engine)


@pytest.mark.asyncio
async def test_a_second_start_leaves_an_edited_row_alone(
    web_login: None, seeded_env: None, test_engine: AsyncEngine
) -> None:
    await _clear_row(test_engine)
    try:
        await seed_admin_settings()
        async with test_engine.begin() as conn:
            await conn.execute(
                text("UPDATE admin_settings SET max_job_cost_usd = 42.5, airgapped = false")
            )
        await seed_admin_settings()
        row = await _row(test_engine)
        assert row["max_job_cost_usd"] == 42.5
        assert row["airgapped"] is False
    finally:
        await _clear_row(test_engine)


@pytest.mark.asyncio
async def test_a_malformed_memory_string_fails_naming_the_variable(
    web_login: None,
    seeded_env: None,
    monkeypatch: pytest.MonkeyPatch,
    test_engine: AsyncEngine,
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_AGENT_MEMORY", "8x")
    clear_settings_cache()
    await _clear_row(test_engine)
    try:
        with pytest.raises(ValueError, match="OPENSCIENTIST_AGENT_MEMORY"):
            await seed_admin_settings()
        assert 0 == await _row_count(test_engine)
    finally:
        await _clear_row(test_engine)


async def _row_count(test_engine: AsyncEngine) -> int:
    async with test_engine.connect() as conn:
        return int(await conn.scalar(text("SELECT count(*) FROM admin_settings")))
