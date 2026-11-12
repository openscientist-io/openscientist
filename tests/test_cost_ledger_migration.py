"""Cost ledger migration preserves attribution and retained expenditure."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine


def _migration() -> ModuleType:
    path = (
        Path(__file__).parent.parent
        / "src/openscientist/database/migrations/versions/2026-11-11_1300_durable_cost_ledger.py"
    )
    spec = importlib.util.spec_from_file_location("durable_cost_ledger", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.asyncio
async def test_cost_migration_preserves_policy_roles(test_engine: AsyncEngine) -> None:
    migration = _migration()
    shared_policies = {
        "cost_records_agent_job": ["openscientist_agent"],
        "cost_records_agent_only_own_job": ["openscientist_agent"],
        "cost_records_delete_shared_edit": ["openscientist_app"],
        "cost_records_modify_shared_edit": ["openscientist_app"],
        "cost_records_select_shared": ["openscientist_app"],
        "cost_records_update_shared_edit": ["openscientist_app"],
    }
    owner_policy = {**shared_policies, "cost_records_all_owner": ["openscientist_app"]}
    ledger_policies = {
        **shared_policies,
        "cost_records_insert_owner": ["openscientist_app"],
        "cost_records_select_owner": ["openscientist_app"],
    }

    def roles(connection: Connection) -> dict[str, list[str]]:
        return dict(
            connection.execute(
                text(
                    "SELECT policyname, roles FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = 'cost_records'"
                )
            )
            .tuples()
            .all()
        )

    def run(connection: Connection) -> None:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                assert roles(connection) == owner_policy
                migration.upgrade()
                assert roles(connection) == ledger_policies
                migration.downgrade()
                assert roles(connection) == owner_policy
        finally:
            transaction.rollback()

    async with test_engine.connect() as connection:
        await connection.run_sync(run)


@pytest.mark.asyncio
async def test_cost_migration_backfills_owners_and_preserves_deleted_job_spend(
    test_engine: AsyncEngine,
) -> None:
    migration = _migration()

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
