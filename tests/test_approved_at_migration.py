"""The migration that adds ``users.approved_at`` backfills already-approved
users from ``updated_at`` and downgrades cleanly."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

_MIGRATION = (
    Path(__file__).parent.parent
    / "src/openscientist/database/migrations/versions"
    / "2026-10-15_1200_add_approved_at_to_users.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("add_approved_at", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_column(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.columns"
                " WHERE table_name = 'users' AND column_name = 'approved_at')"
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_downgrade_drops_the_column_and_upgrade_backfills_approved_users(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()
    approved_email = f"migrated-approved-{uuid4().hex}@example.com"
    pending_email = f"migrated-pending-{uuid4().hex}@example.com"

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert not _has_column(connection)

            # Insert users as they'd exist the moment before this migration first
            # runs: one already approved, one still pending, neither with an
            # approved_at value since the column doesn't exist yet.
            connection.execute(
                text(
                    "INSERT INTO users (id, email, name, is_approved, updated_at)"
                    " VALUES (gen_random_uuid(), :email, 'Approved',"
                    " true, now() - interval '3 days')"
                ),
                {"email": approved_email},
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, email, name, is_approved)"
                    " VALUES (gen_random_uuid(), :email, 'Pending', false)"
                ),
                {"email": pending_email},
            )

            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            assert _has_column(connection)

            approved_row = connection.execute(
                text("SELECT approved_at, updated_at FROM users WHERE email = :email"),
                {"email": approved_email},
            ).one()
            assert approved_row.approved_at == approved_row.updated_at

            pending_approved_at = connection.execute(
                text("SELECT approved_at FROM users WHERE email = :email"),
                {"email": pending_email},
            ).scalar_one()
            assert pending_approved_at is None
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
