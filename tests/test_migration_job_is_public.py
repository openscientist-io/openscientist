"""Round-trip tests for the public-job-visibility migrations (issue #296).

The session's test schema is built by running Alembic in a subprocess, which
coverage cannot trace and which never runs a downgrade. These tests drive
Alembic in-process against a throwaway database, so each migration's upgrade
and downgrade both run, and check what each leaves behind.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import pytest
from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).parent.parent
COLUMN_REVISION = "add_job_is_public"
BEFORE_COLUMN_REVISION = "add_version_info"
RLS_REVISION = "add_job_public_rls"
PUBLIC_POLICY_COUNT = 11  # jobs + 7 job-scoped tables + 3 junction tables


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_database(url: str, database: str) -> str:
    return url.rsplit("/", 1)[0] + f"/{database}"


async def _execute(dsn: str, statement: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute(statement)
    finally:
        await connection.close()


async def _fetchval(dsn: str, query: str) -> object:
    connection = await asyncpg.connect(dsn)
    try:
        return await connection.fetchval(query)
    finally:
        await connection.close()


@pytest.fixture
def scratch_database_url(test_database_url: str) -> Iterator[str]:
    """Create an empty database beside the test database, and drop it after."""
    admin_dsn = _asyncpg_dsn(test_database_url)
    name = f"migration_{uuid.uuid4().hex[:12]}"
    asyncio.run(_execute(admin_dsn, f'CREATE DATABASE "{name}"'))
    scratch_url = _with_database(test_database_url, name)
    try:
        has_uuidv7 = asyncio.run(
            # to_regprocedure, not to_regproc: PostgreSQL 18 overloads uuidv7
            # (with and without an interval), and to_regproc returns NULL for an
            # overloaded name, which would skip this test exactly where it can run.
            _fetchval(_asyncpg_dsn(scratch_url), "SELECT to_regprocedure('uuidv7()') IS NOT NULL")
        )
        if not has_uuidv7:
            pytest.skip("the schema needs PostgreSQL 18's built-in uuidv7()")
        yield scratch_url
    finally:
        asyncio.run(_execute(admin_dsn, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def _alembic_config() -> Config:
    # Built without the ini file on purpose: env.py applies the ini's logging
    # config when it has one, which would replace the loggers other tests use.
    config = Config()
    config.set_main_option(
        "script_location", str(PROJECT_ROOT / "src/openscientist/database/migrations")
    )
    return config


def _column_count(url: str) -> object:
    return asyncio.run(
        _fetchval(
            _asyncpg_dsn(url),
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name = 'is_public'",
        )
    )


def test_add_job_is_public_adds_and_drops_the_column(
    scratch_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", scratch_database_url)
    config = _alembic_config()

    command.upgrade(config, COLUMN_REVISION)
    assert _column_count(scratch_database_url) == 1

    command.downgrade(config, BEFORE_COLUMN_REVISION)
    assert _column_count(scratch_database_url) == 0

    command.upgrade(config, COLUMN_REVISION)
    assert _column_count(scratch_database_url) == 1


def _rls_state(url: str) -> dict[str, object]:
    dsn = _asyncpg_dsn(url)
    return {
        "policies": asyncio.run(
            _fetchval(
                dsn, "SELECT count(*) FROM pg_policies WHERE policyname LIKE '%\\_select\\_public'"
            )
        ),
        "trigger": asyncio.run(
            _fetchval(dsn, "SELECT count(*) FROM pg_trigger WHERE tgname = 'jobs_guard_is_public'")
        ),
        "function": asyncio.run(
            _fetchval(dsn, "SELECT to_regproc('jobs_guard_is_public') IS NOT NULL")
        ),
    }


def test_add_job_public_rls_adds_and_removes_the_access_rules(
    scratch_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", scratch_database_url)
    config = _alembic_config()

    command.upgrade(config, RLS_REVISION)
    assert _rls_state(scratch_database_url) == {
        "policies": PUBLIC_POLICY_COUNT,
        "trigger": 1,
        "function": True,
    }

    command.downgrade(config, COLUMN_REVISION)
    assert _rls_state(scratch_database_url) == {"policies": 0, "trigger": 0, "function": False}
    assert _column_count(scratch_database_url) == 1

    command.upgrade(config, RLS_REVISION)
    assert _rls_state(scratch_database_url)["policies"] == PUBLIC_POLICY_COUNT
