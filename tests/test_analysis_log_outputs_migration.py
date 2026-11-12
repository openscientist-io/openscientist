"""Action outputs remain private through schema upgrades and downgrades."""

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
    / "2026-11-12_1200_analysis_log_outputs.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("analysis_log_outputs", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policies(connection: Connection, table: str) -> list[tuple[object, ...]]:
    return [
        (
            row.policyname.removeprefix(table),
            row.permissive,
            tuple(row.roles),
            row.cmd,
            row.qual.replace(table, "job_table") if row.qual else None,
            row.with_check.replace(table, "job_table") if row.with_check else None,
        )
        for row in connection.execute(
            text(
                "SELECT policyname, permissive, roles, cmd, qual, with_check FROM pg_policies"
                " WHERE schemaname = 'public' AND tablename = :table"
                " AND policyname != :public_policy ORDER BY policyname"
            ),
            {"table": table, "public_policy": f"{table}_select_public"},
        )
    ]


@pytest.mark.asyncio
async def test_analysis_log_outputs_preserve_data_and_private_policies(
    test_engine: AsyncEngine,
) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                connection.execute(text("SET LOCAL ROLE openscientist_owner"))
                module.downgrade()
                connection.execute(text("RESET ROLE"))
                user_id, job_id = uuid4(), uuid4()
                connection.execute(
                    text("INSERT INTO users (id, email, name) VALUES (:id, :email, 'Owner')"),
                    {"id": user_id, "email": f"{user_id}@migration.test"},
                )
                connection.execute(
                    text(
                        "INSERT INTO jobs (id, owner_id, research_question)"
                        " VALUES (:id, :owner, 'Outputs')"
                    ),
                    {"id": job_id, "owner": user_id},
                )
                payloads = [
                    '{"output":"private stdout\\nnext line"}',
                    '{"output":""}',
                    '{"output":null}',
                    "{}",
                    None,
                ]
                ids = [uuid4() for _ in payloads]
                for log_id, payload in zip(ids, payloads, strict=True):
                    connection.execute(
                        text(
                            "INSERT INTO analysis_log"
                            " (id, job_id, iteration, step_number, action_type, description, output_data)"
                            " VALUES (:id, :job_id, 1, 1, 'execute_code', 'Run', CAST(:payload AS jsonb))"
                        ),
                        {"id": log_id, "job_id": job_id, "payload": payload},
                    )
                connection.execute(text("SET LOCAL ROLE openscientist_owner"))
                module.upgrade()
                connection.execute(text("RESET ROLE"))
                assert connection.execute(
                    text(
                        "SELECT analysis_log_id, job_id, output FROM analysis_log_outputs WHERE job_id = :job"
                    ),
                    {"job": job_id},
                ).tuples().all() == [(ids[0], job_id, "private stdout\nnext line")]
                assert not connection.execute(
                    text(
                        "SELECT EXISTS (SELECT FROM information_schema.columns"
                        " WHERE table_name = 'analysis_log' AND column_name = 'output_data')"
                    )
                ).scalar_one()
                assert _policies(connection, "analysis_log_outputs") == _policies(
                    connection, "analysis_log"
                )
                assert connection.execute(
                    text(
                        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid = 'analysis_log_outputs'::regclass"
                    )
                ).scalar_one()
                policies = connection.execute(
                    text(
                        "SELECT policyname, permissive, roles, cmd, qual, with_check FROM pg_policies"
                        " WHERE schemaname = 'public' AND tablename = 'analysis_log_outputs' ORDER BY policyname"
                    )
                ).all()
                assert len(policies) == 7
                print("analysis_log_outputs pg_policies", [tuple(policy) for policy in policies])
                for privilege in ("SELECT", "INSERT"):
                    assert connection.execute(
                        text(
                            "SELECT has_table_privilege('openscientist_agent', 'analysis_log_outputs', :privilege)"
                        ),
                        {"privilege": privilege},
                    ).scalar_one()
                for privilege in ("UPDATE", "DELETE"):
                    assert not connection.execute(
                        text(
                            "SELECT has_table_privilege('openscientist_agent', 'analysis_log_outputs', :privilege)"
                        ),
                        {"privilege": privilege},
                    ).scalar_one()
                connection.execute(text("SET LOCAL ROLE openscientist_owner"))
                module.downgrade()
                connection.execute(text("RESET ROLE"))
                restored = dict(
                    connection.execute(
                        text("SELECT id, output_data FROM analysis_log WHERE job_id = :job"),
                        {"job": job_id},
                    )
                    .tuples()
                    .all()
                )
                assert restored == {
                    log_id: {"output": "private stdout\nnext line"} if n == 0 else None
                    for n, log_id in enumerate(ids)
                }
                assert (
                    connection.execute(
                        text("SELECT to_regclass('analysis_log_outputs')")
                    ).scalar_one()
                    is None
                )
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
