"""The migration that drops the draft schema downgrades to a schema the code of
the previous release can use, and upgrades back."""

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
    / "2026-10-31_1200_drop_draft_schema.py"
)

_DROPPED = {
    ("jobs", "resume_iteration"),
    ("sessions", "ip_address"),
    ("sessions", "user_agent"),
    ("iteration_summaries", "key_findings"),
    ("iteration_summaries", "hypotheses_generated"),
    ("iteration_summaries", "hypotheses_tested"),
    ("iteration_summaries", "code_executed"),
    ("iteration_summaries", "new_insights"),
    ("iteration_summaries", "next_steps"),
    ("iteration_summaries", "metrics"),
    ("literature", "journal"),
    ("literature", "doi"),
    ("literature", "relevance_score"),
    ("literature", "citation_key"),
    ("feedback_history", "response_text"),
    ("plots", "plot_type"),
    ("plots", "code_used"),
}


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("drop_draft_schema", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _present_columns(connection: Connection) -> set[tuple[str, str]]:
    rows = connection.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns"
            " WHERE table_schema = 'public'"
        )
    ).all()
    return {(row.table_name, row.column_name) for row in rows} & _DROPPED


def _spawn_table_security(connection: Connection) -> tuple[bool, bool] | None:
    row = connection.execute(
        text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE oid = to_regclass('public.hypothesis_spawns')"
        )
    ).one_or_none()
    return None if row is None else (row.relrowsecurity, row.relforcerowsecurity)


def _agent_may_update(connection: Connection, column: str) -> bool:
    return bool(
        connection.execute(
            text("SELECT has_column_privilege('openscientist_agent', 'literature', :c, 'UPDATE')"),
            {"c": column},
        ).scalar_one()
    )


@pytest.mark.asyncio
async def test_downgrade_restores_the_draft_schema_and_upgrade_drops_it(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            assert _present_columns(connection) == set()
            assert _spawn_table_security(connection) is None
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert _present_columns(connection) == _DROPPED
                assert _spawn_table_security(connection) == (True, True)
                assert _agent_may_update(connection, "journal")
                assert _agent_may_update(connection, "doi")
                module.upgrade()
            assert _present_columns(connection) == set()
            assert _spawn_table_security(connection) is None
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
