"""The migration that adds ``jobs.failed_phase`` backfills failed jobs, grants
both writers the column, and downgrades cleanly."""

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
    / "2026-11-18_1200_job_failed_phase.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("job_failed_phase", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_column(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.columns"
                " WHERE table_name = 'jobs' AND column_name = 'failed_phase')"
            )
        ).scalar_one()
    )


def _update_grantees(connection: Connection) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT grantee FROM information_schema.column_privileges"
            " WHERE table_schema = 'public' AND table_name = 'jobs'"
            " AND column_name = 'failed_phase' AND privilege_type = 'UPDATE'"
        )
    )
    return {row[0] for row in rows}


def _job(connection: Connection, status: str, error: str | None) -> str:
    return str(
        connection.execute(
            text(
                "INSERT INTO jobs (id, research_question, status, error_message)"
                " VALUES (gen_random_uuid(), 'Backfilled?', :status, :error) RETURNING id"
            ),
            {"status": status, "error": error},
        ).scalar_one()
    )


def _phase(connection: Connection, job_id: str) -> str | None:
    phase: str | None = connection.execute(
        text("SELECT failed_phase FROM jobs WHERE id = :id"), {"id": job_id}
    ).scalar_one()
    return phase


@pytest.mark.asyncio
async def test_failed_phase_backfills_grants_and_downgrades(test_engine: AsyncEngine) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert not _has_column(connection)
                in_report = _job(connection, "failed", "Report generation failed: API Error")
                in_iterations = _job(connection, "failed", "Agent container exited with code 1")
                without_error = _job(connection, "failed", None)
                completed = _job(connection, "completed", None)
                module.upgrade()
            assert _phase(connection, in_report) == "report"
            assert _phase(connection, in_iterations) == "iterations"
            assert _phase(connection, without_error) == "iterations"
            assert _phase(connection, completed) is None
            assert {"openscientist_agent", "openscientist_app"} <= _update_grantees(connection)
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
