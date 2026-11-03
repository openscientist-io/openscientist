"""The web refuses to start while a table that holds per-user rows lacks enforced RLS."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from openscientist import web_app


async def test_a_fully_migrated_database_passes(test_engine: AsyncEngine) -> None:
    await web_app._verify_db_connection_and_rls(test_engine)


@pytest.mark.parametrize(
    ("table", "weaken", "restore"),
    [
        # A table the initial migration protects one by one.
        ("users", "DISABLE ROW LEVEL SECURITY", "ENABLE ROW LEVEL SECURITY"),
        # A table protected through the shared job-related policy helper.
        ("findings", "NO FORCE ROW LEVEL SECURITY", "FORCE ROW LEVEL SECURITY"),
    ],
)
async def test_a_table_without_enforced_rls_stops_the_web(
    test_engine: AsyncEngine, table: str, weaken: str, restore: str
) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text(f"ALTER TABLE {table} {weaken}"))
    try:
        with pytest.raises(web_app.RlsNotEnforcedError, match=table):
            await web_app._verify_db_connection_and_rls(test_engine)
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE {table} {restore}"))


@pytest.mark.parametrize(
    ("weaken", "restore", "named"),
    [
        ("SUPERUSER", "NOSUPERUSER", "SUPERUSER"),
        ("BYPASSRLS", "NOBYPASSRLS", "BYPASSRLS"),
    ],
)
async def test_an_app_role_that_bypasses_rls_stops_the_web(
    test_engine: AsyncEngine, weaken: str, restore: str, named: str
) -> None:
    async with test_engine.begin() as conn:
        await conn.execute(text(f"ALTER ROLE openscientist_app {weaken}"))
    try:
        with pytest.raises(web_app.RlsNotEnforcedError, match=named):
            await web_app._verify_db_connection_and_rls(test_engine)
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text(f"ALTER ROLE openscientist_app {restore}"))


async def test_a_missing_app_role_stops_the_web(
    test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real role owns grants and policies, so the check is pointed at a
    role name that does not exist."""
    monkeypatch.setattr(web_app, "APP_ROLE", "openscientist_app_that_does_not_exist")
    with pytest.raises(web_app.RlsNotEnforcedError, match="does not exist"):
        await web_app._verify_db_connection_and_rls(test_engine)


async def test_the_lifespan_lets_the_rls_failure_stop_the_web(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup logs most background-task errors and carries on, so the RLS
    failure must pass through it the way a port that cannot be bound does."""
    from fastapi import FastAPI

    from openscientist.database import engine

    async def unprotected(_engine: AsyncEngine) -> None:
        raise web_app.RlsNotEnforcedError("users")

    monkeypatch.setattr(engine, "get_engine", lambda: None)
    monkeypatch.setattr(web_app, "_verify_db_connection_and_rls", unprotected)
    with pytest.raises(web_app.RlsNotEnforcedError):
        async with web_app._create_lifespan()(FastAPI()):
            pass


async def test_an_unreachable_database_does_not_stop_the_web(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    try:
        with caplog.at_level(logging.ERROR):
            await web_app._verify_db_connection_and_rls(engine)
    finally:
        await engine.dispose()
    assert "Database connection failed" in caplog.text
