"""The reader returns the row as a running part reads it, and the environment's
airgap floor wins over the row."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.admin_settings import (
    AdminSettingsMissingError,
    AdminSettingsValues,
    current_admin_settings,
    current_admin_settings_sync,
)
from openscientist.settings import clear_settings_cache
from tests.admin_row import reset_admin_settings_row

_UPDATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

_ROW = {
    "max_job_cost_usd": 12.5,
    "max_project_spend_month_usd": None,
    "max_project_spend_24h_usd": 500.0,
    "warn_project_spend_24h_usd": 100.0,
    "agent_cpu": 2.0,
    "executor_cpu": 0.5,
    "agent_memory_mib": 8192,
    "executor_memory_mib": 512,
    "agent_timeout_seconds": 14400,
    "executor_timeout_seconds": 120,
    "max_file_size_mb": 1000,
    "max_concurrent_jobs": 0,
    "scheduler_window_hours": 24.0,
    "session_duration_days": 30,
}

_INSERT = """
    INSERT INTO admin_settings (
        max_job_cost_usd, max_project_spend_month_usd, max_project_spend_24h_usd,
        warn_project_spend_24h_usd, agent_cpu, executor_cpu, agent_memory_mib,
        executor_memory_mib, agent_timeout_seconds, executor_timeout_seconds,
        max_file_size_mb, max_concurrent_jobs, scheduler_window_hours,
        airgapped, session_duration_days, updated_at, updated_by
    ) VALUES (
        :max_job_cost_usd, :max_project_spend_month_usd, :max_project_spend_24h_usd,
        :warn_project_spend_24h_usd, :agent_cpu, :executor_cpu, :agent_memory_mib,
        :executor_memory_mib, :agent_timeout_seconds, :executor_timeout_seconds,
        :max_file_size_mb, :max_concurrent_jobs, :scheduler_window_hours,
        :airgapped, :session_duration_days, :updated_at, :updated_by
    )
"""


@pytest.fixture
def no_floor(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """The environment without the airgap floor."""
    monkeypatch.setenv("OPENSCIENTIST_AIRGAPPED", "false")
    clear_settings_cache()
    yield
    clear_settings_cache()


async def _write_row(
    test_engine: AsyncEngine, *, airgapped: bool, updated_by: UUID | None = None
) -> None:
    values = {**_ROW, "airgapped": airgapped, "updated_at": _UPDATED_AT, "updated_by": updated_by}
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM admin_settings"))
        await conn.execute(text(_INSERT), values)


async def _clear_row(test_engine: AsyncEngine) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text("DELETE FROM admin_settings"))


async def _make_user(test_engine: AsyncEngine) -> UUID:
    async with test_engine.begin() as conn:
        result = await conn.execute(
            text("INSERT INTO users (email, name) VALUES (:email, 'R') RETURNING id"),
            {"email": f"reader-{uuid4().hex}@example.com"},
        )
        user_id: UUID = result.scalar_one()
        return user_id


@pytest.mark.asyncio
async def test_the_reader_returns_the_row(
    web_login: None, no_floor: None, test_engine: AsyncEngine
) -> None:
    user = await _make_user(test_engine)
    await _write_row(test_engine, airgapped=False, updated_by=user)
    try:
        values = await current_admin_settings()
        assert values == AdminSettingsValues(
            max_job_cost_usd=12.5,
            max_project_spend_month_usd=None,
            max_project_spend_24h_usd=500.0,
            warn_project_spend_24h_usd=100.0,
            agent_cpu=2.0,
            executor_cpu=0.5,
            agent_memory_mib=8192,
            executor_memory_mib=512,
            agent_timeout_seconds=14400,
            executor_timeout_seconds=120,
            max_file_size_mb=1000,
            max_concurrent_jobs=0,
            scheduler_window_hours=24.0,
            airgapped=False,
            airgap_locked=False,
            session_duration_days=30,
            updated_at=_UPDATED_AT,
            updated_by=user,
        )
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})
        await reset_admin_settings_row(test_engine)


@pytest.mark.asyncio
async def test_the_floor_forces_airgapped_and_locks_it(
    web_login: None, monkeypatch: pytest.MonkeyPatch, test_engine: AsyncEngine
) -> None:
    await _write_row(test_engine, airgapped=False)
    try:
        monkeypatch.setenv("OPENSCIENTIST_AIRGAPPED", "true")
        clear_settings_cache()
        values = await current_admin_settings()
        assert values.airgapped is True
        assert values.airgap_locked is True
    finally:
        clear_settings_cache()
        await reset_admin_settings_row(test_engine)


@pytest.mark.asyncio
async def test_the_row_wins_without_the_floor(
    web_login: None, no_floor: None, test_engine: AsyncEngine
) -> None:
    await _write_row(test_engine, airgapped=True)
    try:
        values = await current_admin_settings()
        assert values.airgapped is True
        assert values.airgap_locked is False
    finally:
        await reset_admin_settings_row(test_engine)


@pytest.mark.asyncio
async def test_a_missing_row_raises(
    web_login: None, no_floor: None, test_engine: AsyncEngine
) -> None:
    await _clear_row(test_engine)
    try:
        with pytest.raises(AdminSettingsMissingError):
            await current_admin_settings()
    finally:
        await reset_admin_settings_row(test_engine)


@pytest.mark.asyncio
async def test_the_sync_form_runs_from_a_worker_thread(
    web_login: None, no_floor: None, test_engine: AsyncEngine
) -> None:
    await _write_row(test_engine, airgapped=False)
    try:
        values = await asyncio.to_thread(current_admin_settings_sync)
        assert values.agent_cpu == 2.0
        assert values.max_project_spend_month_usd is None
        assert values.airgap_locked is False
    finally:
        await reset_admin_settings_row(test_engine)


@pytest.mark.asyncio
async def test_cost_visibility_follows_live_policy(
    web_login: None, no_floor: None, test_engine: AsyncEngine
) -> None:
    from openscientist.admin_settings import costs_visible
    from tests.admin_row import set_admin_settings_row

    try:
        await reset_admin_settings_row(test_engine)
        assert await costs_visible(False)
        await set_admin_settings_row(test_engine, show_job_costs=False)
        assert not await costs_visible(False)
        assert await costs_visible(True)
        await set_admin_settings_row(test_engine, show_job_costs=True)
        assert await costs_visible(False)
    finally:
        await reset_admin_settings_row(test_engine)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (None, None),
        (
            "Job cost limit of $12.00 reached: the job has spent $12.50.",
            "The job reached its cost limit.",
        ),
        (
            "Project monthly spend limit of $100.00 reached: the project has spent $101.00.",
            "The project reached its monthly cost limit.",
        ),
        (
            "Project 24-hour spend limit of $10.00 reached: the project has spent $11.00.",
            "The project reached its 24-hour cost limit.",
        ),
        ("The budget could not be checked.", "The budget could not be checked."),
        ("Samples cost $12.00.", "Samples cost $12.00."),
    ],
)
def test_only_generated_budget_messages_hide_amounts(
    message: str | None, expected: str | None
) -> None:
    from openscientist.admin_settings import hide_cost_amounts

    assert hide_cost_amounts(message) == expected
