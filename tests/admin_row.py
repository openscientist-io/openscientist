"""Hold and edit the admin settings row for tests.

Invariant: between tests the test database holds the default row, the way a
running deployment holds it after startup seeding. ``reset_admin_settings_row``
restores that state; a test that edits the row resets it in its teardown, and
a test that needs the row absent deletes it in its own body and restores the
defaults before returning.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.admin_settings import AdminSettingsValues

_ROW_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

_DEFAULTS: dict[str, object] = {
    "max_job_cost_usd": None,
    "max_project_spend_total_usd": None,
    "max_project_spend_24h_usd": None,
    "warn_project_spend_24h_usd": None,
    "agent_cpu": 2.0,
    "executor_cpu": 0.5,
    "agent_memory_mib": 8192,
    "executor_memory_mib": 512,
    "agent_timeout_seconds": 14400,
    "executor_timeout_seconds": 120,
    "max_file_size_mb": 1000,
    "max_concurrent_jobs": 1,
    "scheduler_window_hours": 24.0,
    "airgapped": False,
    "session_duration_days": 30,
}

_INSERT = """
    INSERT INTO admin_settings (
        max_job_cost_usd, max_project_spend_total_usd, max_project_spend_24h_usd,
        warn_project_spend_24h_usd, agent_cpu, executor_cpu, agent_memory_mib,
        executor_memory_mib, agent_timeout_seconds, executor_timeout_seconds,
        max_file_size_mb, max_concurrent_jobs, scheduler_window_hours,
        airgapped, session_duration_days, updated_at, updated_by
    ) VALUES (
        :max_job_cost_usd, :max_project_spend_total_usd, :max_project_spend_24h_usd,
        :warn_project_spend_24h_usd, :agent_cpu, :executor_cpu, :agent_memory_mib,
        :executor_memory_mib, :agent_timeout_seconds, :executor_timeout_seconds,
        :max_file_size_mb, :max_concurrent_jobs, :scheduler_window_hours,
        :airgapped, :session_duration_days, :updated_at, :updated_by
    )
"""


def _row_values(**overrides: object) -> dict[str, object]:
    return {**_DEFAULTS, **overrides, "updated_at": _ROW_TIME, "updated_by": None}


async def reset_admin_settings_row(engine: AsyncEngine) -> None:
    """Write the default row, the state the suite holds between tests."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM admin_settings"))
        await conn.execute(text(_INSERT), _row_values())


async def set_admin_settings_row(engine: AsyncEngine, **overrides: object) -> None:
    """Replace the row with the defaults plus ``overrides``."""
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM admin_settings"))
        await conn.execute(text(_INSERT), _row_values(**overrides))


_BASE_VALUES = AdminSettingsValues(
    max_job_cost_usd=None,
    max_project_spend_total_usd=None,
    max_project_spend_24h_usd=None,
    warn_project_spend_24h_usd=None,
    agent_cpu=2.0,
    executor_cpu=0.5,
    agent_memory_mib=8192,
    executor_memory_mib=512,
    agent_timeout_seconds=14400,
    executor_timeout_seconds=120,
    max_file_size_mb=1000,
    max_concurrent_jobs=1,
    scheduler_window_hours=24.0,
    airgapped=False,
    airgap_locked=False,
    session_duration_days=30,
    updated_at=_ROW_TIME,
    updated_by=None,
)


def default_admin_settings_values() -> AdminSettingsValues:
    """The default row as values, for patching the reader where a test has
    no database to hold the row."""
    return _BASE_VALUES
