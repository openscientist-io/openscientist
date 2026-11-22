"""The migration that lets admins grant and remove admin rights upgrades and
downgrades cleanly."""

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
    / "2026-11-21_1200_admins_grant_admins.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("admins_grant_admins", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(connection: Connection) -> tuple[set[str], bool, bool]:
    """The table's policies, whether its trigger exists, and whether the app role may UPDATE."""
    policies = set(
        connection.execute(
            text(
                "SELECT policyname FROM pg_policies"
                " WHERE schemaname = 'public' AND tablename = 'administrators'"
            )
        ).scalars()
    )
    trigger = connection.execute(
        text(
            "SELECT count(*) FROM pg_trigger"
            " WHERE tgrelid = 'public.administrators'::regclass"
            " AND tgname = 'administrators_actor_check'"
        )
    ).scalar_one()
    update = connection.execute(
        text("SELECT has_table_privilege('openscientist_app', 'public.administrators', 'UPDATE')")
    ).scalar_one()
    return policies, bool(trigger), bool(update)


@pytest.mark.asyncio
async def test_admins_grant_admins_upgrades_and_downgrades_cleanly(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert _state(connection) == ({"administrators_select_admin"}, False, True)
                module.upgrade()
            assert _state(connection) == (
                {
                    "administrators_select_admin",
                    "administrators_insert_admin",
                    "administrators_delete_admin",
                },
                True,
                False,
            )
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
