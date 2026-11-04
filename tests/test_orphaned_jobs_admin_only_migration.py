"""The migration that restricts the orphaned-job select policy to admins
upgrades and downgrades cleanly."""

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
    / "2026-11-03_1200_orphaned_jobs_admin_only.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("orphaned_jobs_admin_only", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _orphan_policy_using(connection: Connection) -> str:
    value = connection.execute(
        text(
            "SELECT qual FROM pg_policies"
            " WHERE schemaname = 'public' AND policyname = 'jobs_select_orphaned'"
        )
    ).scalar_one()
    return str(value)


@pytest.mark.asyncio
async def test_orphaned_jobs_admin_only_upgrades_and_downgrades_cleanly(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                using = _orphan_policy_using(connection)
                assert "owner_id IS NULL" in using
                assert "current_user_is_admin" not in using
                module.upgrade()
            using = _orphan_policy_using(connection)
            assert "owner_id IS NULL" in using
            assert "current_user_is_admin" in using
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
