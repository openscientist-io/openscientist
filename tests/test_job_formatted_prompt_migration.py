"""The migration that adds ``jobs.formatted_prompt`` also grants the container
role its write, and downgrades cleanly."""

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
    / "2026-11-07_1200_job_formatted_prompt.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("job_formatted_prompt", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_column(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.columns"
                " WHERE table_name = 'jobs' AND column_name = 'formatted_prompt')"
            )
        ).scalar_one()
    )


def _update_columns(connection: Connection, role: str) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT column_name FROM information_schema.column_privileges"
            " WHERE table_schema = 'public' AND table_name = 'jobs'"
            " AND grantee = :role AND privilege_type = 'UPDATE'"
        ),
        {"role": role},
    )
    return {row[0] for row in rows}


@pytest.mark.asyncio
async def test_formatted_prompt_upgrades_and_downgrades_cleanly(test_engine: AsyncEngine) -> None:
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
            assert "formatted_prompt" in _update_columns(connection, "openscientist_agent")
            # Only the container writes it, so a share with edit rights cannot.
            assert "formatted_prompt" not in _update_columns(connection, "openscientist_app")
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
