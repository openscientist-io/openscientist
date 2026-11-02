"""The migration that adds ``jobs.stop_note`` also grants the container role its
write, and downgrades cleanly."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

_MIGRATION = (
    Path(__file__).parent.parent
    / "src/openscientist/database/migrations/versions"
    / "2026-11-01_1300_job_stop_note.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("job_stop_note", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_column(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.columns"
                " WHERE table_name = 'jobs' AND column_name = 'stop_note')"
            )
        ).scalar_one()
    )


def _agent_update_columns(connection: Connection) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT column_name FROM information_schema.column_privileges"
            " WHERE table_schema = 'public' AND table_name = 'jobs'"
            " AND grantee = 'openscientist_agent' AND privilege_type = 'UPDATE'"
        )
    )
    return {row[0] for row in rows}


@pytest.mark.asyncio
async def test_stop_note_upgrades_and_downgrades_cleanly(test_engine: AsyncEngine) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert not _has_column(connection)
                module.upgrade()
            assert _has_column(connection)
            assert "stop_note" in _agent_update_columns(connection)
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
