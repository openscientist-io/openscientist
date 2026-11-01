"""The admin settings migration creates the row and history with their bounds,
and downgrades cleanly."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

_MIGRATION = (
    Path(__file__).parent.parent
    / "src/openscientist/database/migrations/versions"
    / "2026-10-31_1300_admin_settings.py"
)

_ADMIN_SETTINGS_COLUMNS = {
    "id",
    "max_job_cost_usd",
    "max_project_spend_total_usd",
    "max_project_spend_24h_usd",
    "warn_project_spend_24h_usd",
    "agent_cpu",
    "executor_cpu",
    "agent_memory_mib",
    "executor_memory_mib",
    "agent_timeout_seconds",
    "executor_timeout_seconds",
    "max_file_size_mb",
    "max_concurrent_jobs",
    "scheduler_window_hours",
    "airgapped",
    "session_duration_days",
    "updated_at",
    "updated_by",
}
_CHANGE_COLUMNS = {"id", "changed_at", "changed_by", "setting", "old_value", "new_value"}

# NULL budgets mean no limit, 0 pauses new starts, and 1 day is the shortest session.
_VALID_ROW: dict[str, object] = {
    "max_job_cost_usd": None,
    "max_project_spend_total_usd": None,
    "max_project_spend_24h_usd": None,
    "warn_project_spend_24h_usd": None,
    "agent_cpu": 1e-9,
    "executor_cpu": 1e-9,
    "agent_memory_mib": 1,
    "executor_memory_mib": 1,
    "agent_timeout_seconds": 1,
    "executor_timeout_seconds": 1,
    "max_file_size_mb": 1,
    "max_concurrent_jobs": 0,
    "scheduler_window_hours": 1e-9,
    "airgapped": False,
    "session_duration_days": 1,
}

_INSERT = """
    INSERT INTO admin_settings (
        max_job_cost_usd, max_project_spend_total_usd, max_project_spend_24h_usd,
        warn_project_spend_24h_usd, agent_cpu, executor_cpu, agent_memory_mib,
        executor_memory_mib, agent_timeout_seconds, executor_timeout_seconds,
        max_file_size_mb, max_concurrent_jobs, scheduler_window_hours,
        airgapped, session_duration_days
    ) VALUES (
        :max_job_cost_usd, :max_project_spend_total_usd, :max_project_spend_24h_usd,
        :warn_project_spend_24h_usd, :agent_cpu, :executor_cpu, :agent_memory_mib,
        :executor_memory_mib, :agent_timeout_seconds, :executor_timeout_seconds,
        :max_file_size_mb, :max_concurrent_jobs, :scheduler_window_hours,
        :airgapped, :session_duration_days
    )
"""


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("admin_settings", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table_columns(connection: Connection, table: str) -> set[str] | None:
    rows = connection.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
        {"table": table},
    ).all()
    return None if not rows else {row.column_name for row in rows}


def _insert_row(connection: Connection, **overrides: object) -> None:
    connection.execute(text("DELETE FROM admin_settings"))
    connection.execute(text(_INSERT), {**_VALID_ROW, **overrides})


# A constraint violation aborts the transaction, so each violation rolls back
# to its own savepoint.
def _violates(connection: Connection, statement: str, params: dict[str, object] | None) -> None:
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(text(statement), params)


@pytest.mark.asyncio
async def test_downgrade_drops_both_tables_and_upgrade_creates_them(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert _table_columns(connection, "admin_settings") is None
                assert _table_columns(connection, "admin_setting_changes") is None
                module.upgrade()
                assert _table_columns(connection, "admin_settings") == _ADMIN_SETTINGS_COLUMNS
                assert _table_columns(connection, "admin_setting_changes") == _CHANGE_COLUMNS
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)


@pytest.mark.asyncio
async def test_the_row_is_single(test_engine: AsyncEngine) -> None:
    def run(connection: Connection) -> None:
        transaction = connection.begin()
        try:
            _insert_row(connection)
            _violates(connection, _INSERT, _VALID_ROW)
            _violates(connection, "INSERT INTO admin_settings (id) VALUES (false)", None)
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)


@pytest.mark.asyncio
async def test_the_bounds_accept_their_edges(test_engine: AsyncEngine) -> None:
    """A NULL budget, 0 concurrency, a 1 day session and 1 MiB of memory pass."""

    def run(connection: Connection) -> None:
        transaction = connection.begin()
        try:
            _insert_row(connection)
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("max_job_cost_usd", 0.0),
        ("max_project_spend_total_usd", 0.0),
        ("max_project_spend_24h_usd", 0.0),
        ("warn_project_spend_24h_usd", 0.0),
        ("agent_cpu", 0.0),
        ("executor_cpu", 0.0),
        ("agent_memory_mib", 0),
        ("executor_memory_mib", 0),
        ("agent_timeout_seconds", 0),
        ("executor_timeout_seconds", 0),
        ("max_file_size_mb", 0),
        ("max_concurrent_jobs", -1),
        ("scheduler_window_hours", 0.0),
        ("session_duration_days", 0),
    ],
)
@pytest.mark.asyncio
async def test_the_checks_reject_their_out_of_bounds_value(
    test_engine: AsyncEngine, column: str, value: object
) -> None:
    def run(connection: Connection) -> None:
        transaction = connection.begin()
        try:
            connection.execute(text("DELETE FROM admin_settings"))
            _violates(connection, _INSERT, {**_VALID_ROW, column: value})
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
