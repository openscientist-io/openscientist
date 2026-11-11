"""Session credential migrations invalidate cookies in both directions."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_session_token_migration_invalidates_existing_sessions(
    test_engine: AsyncEngine,
) -> None:
    path = (
        Path(__file__).parent.parent
        / "src/openscientist/database/migrations/versions/2026-11-11_1200_session_token_hash.py"
    )
    spec = importlib.util.spec_from_file_location("session_token_hash", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def run(connection: Connection) -> None:
        transaction = connection.begin()
        try:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                user_id = connection.execute(
                    text(
                        "INSERT INTO users (email, name) VALUES ('migration@session.test', 'Test') RETURNING id"
                    )
                ).scalar_one()
                connection.execute(
                    text(
                        "INSERT INTO sessions (user_id, expires_at) VALUES (:user_id, now() + interval '1 day')"
                    ),
                    {"user_id": user_id},
                )
                migration.upgrade()
                assert connection.scalar(text("SELECT count(*) FROM sessions")) == 0
                connection.execute(
                    text(
                        "INSERT INTO sessions (user_id, expires_at, token_hash) VALUES (:user_id, now() + interval '1 day', :hash)"
                    ),
                    {"user_id": user_id, "hash": "a" * 64},
                )
                migration.downgrade()
                assert connection.scalar(text("SELECT count(*) FROM sessions")) == 0
        finally:
            transaction.rollback()

    async with test_engine.connect() as connection:
        await connection.run_sync(run)
