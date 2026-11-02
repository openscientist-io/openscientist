"""The migration that adds ``jobs.is_public``, the public-read policies and the
owner-only trigger upgrades and downgrades cleanly."""

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
    / "2026-11-02_1200_job_is_public.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("job_is_public", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_column(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.columns"
                " WHERE table_name = 'jobs' AND column_name = 'is_public')"
            )
        ).scalar_one()
    )


def _public_policy_count(connection: Connection) -> int:
    # jobs + 7 job-scoped tables + 2 junction tables.
    return (
        connection.execute(
            text(
                "SELECT count(*) FROM pg_policies"
                " WHERE schemaname = 'public' AND policyname LIKE '%\\_select\\_public'"
            )
        ).scalar_one()
        or 0
    )


def _trigger_count(connection: Connection) -> int:
    return (
        connection.execute(
            text("SELECT count(*) FROM pg_trigger WHERE tgname = 'jobs_guard_is_public'")
        ).scalar_one()
        or 0
    )


def _guard_function_exists(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT count(*) FROM pg_proc p"
                " JOIN pg_namespace n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'public' AND p.proname = 'jobs_guard_is_public'"
            )
        ).scalar_one()
        > 0
    )


@pytest.mark.asyncio
async def test_job_is_public_upgrades_and_downgrades_cleanly(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert not _has_column(connection)
                assert _public_policy_count(connection) == 0
                assert _trigger_count(connection) == 0
                assert not _guard_function_exists(connection)
                module.upgrade()
            assert _has_column(connection)
            assert _public_policy_count(connection) == 10
            assert _trigger_count(connection) == 1
            assert _guard_function_exists(connection)
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
