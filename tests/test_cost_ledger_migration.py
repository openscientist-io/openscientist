"""Cost ledger migration preserves attribution and retained expenditure."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_cost_migration_backfills_owners_and_preserves_deleted_job_spend(
    test_engine: AsyncEngine,
) -> None:
    path = (
        Path(__file__).parent.parent
        / "src/openscientist/database/migrations/versions/2026-11-11_1300_durable_cost_ledger.py"
    )
    spec = importlib.util.spec_from_file_location("durable_cost_ledger", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection: Connection) -> None:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                user_id = connection.scalar(
                    text(
                        "INSERT INTO users (email, name) VALUES ('migration@cost.test', 'Migration owner') RETURNING id"
                    )
                )
                job_id = connection.scalar(
                    text(
                        "INSERT INTO jobs (owner_id, research_question) VALUES (:user, 'Ledger migration') RETURNING id"
                    ),
                    {"user": user_id},
                )
                cost_id = connection.scalar(
                    text(
                        "INSERT INTO cost_records (job_id, operation_type, provider, model, input_tokens, output_tokens, cost_usd) VALUES (:job, 'chat', 'anthropic', 'claude-sonnet-4-6', 1000, 0, 3) RETURNING id"
                    ),
                    {"job": job_id},
                )
                connection.execute(text("SET LOCAL ROLE openscientist_owner"))
                migration.upgrade()
                connection.execute(text("RESET ROLE"))
                assert (
                    connection.scalar(
                        text("SELECT user_id FROM cost_records WHERE id = :id"), {"id": cost_id}
                    )
                    == user_id
                )
                connection.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": job_id})
                assert (
                    connection.scalar(
                        text("SELECT cost_usd FROM cost_records WHERE id = :id"), {"id": cost_id}
                    )
                    == 3.0
                )
                assert (
                    connection.scalar(
                        text("SELECT job_id FROM cost_records WHERE id = :id"), {"id": cost_id}
                    )
                    is None
                )
                with pytest.raises(DBAPIError, match="Cannot downgrade"):
                    with connection.begin_nested():
                        connection.execute(text("SET LOCAL ROLE openscientist_owner"))
                        migration.downgrade()
                assert (
                    connection.scalar(
                        text("SELECT cost_usd FROM cost_records WHERE id = :id"), {"id": cost_id}
                    )
                    == 3.0
                )
        finally:
            transaction.rollback()

    async with test_engine.connect() as connection:
        await connection.run_sync(run)
