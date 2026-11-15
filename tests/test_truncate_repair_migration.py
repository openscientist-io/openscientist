"""The migration that takes ``TRUNCATE`` from the app role again, on installs
whose bootstrap re-ran after the narrowing migration."""

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
    / "2026-11-15_1200_revoke_app_truncate_again.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("revoke_app_truncate_again", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_truncate_leaves_the_app_role_on_existing_and_later_tables(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()

    def run(connection: Connection) -> tuple[bool, bool]:
        transaction = connection.begin()
        try:
            # The default privileges a bootstrap run before the fix left behind, and
            # a table a later migration created under them.
            connection.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE openscientist_owner IN SCHEMA public "
                    "GRANT TRUNCATE ON TABLES TO openscientist_app"
                )
            )
            connection.execute(text("GRANT TRUNCATE ON analysis_log_outputs TO openscientist_app"))
            connection.execute(text("SET LOCAL ROLE openscientist_owner"))
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
            connection.execute(text("CREATE TABLE created_after_repair (id int)"))
            existing, later = connection.execute(
                text(
                    "SELECT has_table_privilege('openscientist_app', 'analysis_log_outputs', "
                    "'TRUNCATE'), has_table_privilege('openscientist_app', "
                    "'created_after_repair', 'TRUNCATE')"
                )
            ).one()
            return bool(existing), bool(later)
        finally:
            transaction.rollback()

    async with test_engine.connect() as connection:
        assert await connection.run_sync(run) == (False, False)
