"""The migration that adds ``users.is_priority`` upgrades existing users to not
priority and downgrades cleanly."""

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
    / "2026-10-30_1200_user_priority.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("user_priority", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_column(connection: Connection) -> bool:
    return bool(
        connection.execute(
            text(
                "SELECT EXISTS (SELECT FROM information_schema.columns"
                " WHERE table_name = 'users' AND column_name = 'is_priority')"
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_downgrade_drops_the_flag_and_upgrade_leaves_every_user_unprioritised(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()
    email = f"migrated-{uuid4().hex}@example.com"

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, name, is_priority)"
                    " VALUES (gen_random_uuid(), :email, 'M', true)"
                ),
                {"email": email},
            )
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert not _has_column(connection)
                module.upgrade()
            assert _has_column(connection)
            flag = connection.execute(
                text("SELECT is_priority FROM users WHERE email = :email"), {"email": email}
            ).scalar_one()
            assert flag is False
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
