"""The admin settings grants. Every user's request reads the row through the app
role, writes and the history go through the admin login, and the agent role
touches neither table."""

from __future__ import annotations

from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.db_logins import ADMIN_PASSWORD, WEB_PASSWORD, login_url

_ROLE_PASSWORD = "admin-settings-role-test-password"

_SETTINGS_VALUES = "0.5, 0.5, 1024, 1024, 60, 60, 100, 1, 24.0, false, 30, NULL, NULL, NULL, NULL"

_FULL_SETTINGS_INSERT = (
    "INSERT INTO admin_settings (agent_cpu, executor_cpu, agent_memory_mib, "
    "executor_memory_mib, agent_timeout_seconds, executor_timeout_seconds, "
    "max_file_size_mb, max_concurrent_jobs, scheduler_window_hours, airgapped, "
    "session_duration_days, max_job_cost_usd, max_project_spend_total_usd, "
    "max_project_spend_24h_usd, warn_project_spend_24h_usd) VALUES (" + _SETTINGS_VALUES + ")"
)


async def _connect(database_url: str) -> asyncpg.Connection:
    url = make_url(database_url)
    return await asyncpg.connect(
        host=url.host,
        port=url.port,
        database=url.database,
        user=url.username,
        password=url.password,
    )


@pytest.mark.asyncio
async def test_the_app_role_reads_the_row_and_cannot_write_it(
    test_database_url: str, _apply_migrations_once: None
) -> None:
    conn = await _connect(login_url(test_database_url, "openscientist_web", WEB_PASSWORD))
    try:
        await conn.execute("SET ROLE openscientist_app")
        await conn.fetchval("SELECT count(*) FROM admin_settings")
        for statement in (
            "UPDATE admin_settings SET airgapped = true",
            "DELETE FROM admin_settings",
            _FULL_SETTINGS_INSERT,
            "SELECT 1 FROM admin_setting_changes LIMIT 1",
            "INSERT INTO admin_setting_changes (setting) VALUES ('airgapped')",
        ):
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_agent_role_touches_neither_table(
    test_database_url: str, test_engine: AsyncEngine, _apply_migrations_once: None
) -> None:
    role = f"admin_settings_{uuid4().hex}"
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{_ROLE_PASSWORD}' IN ROLE openscientist_agent"
            )
        )
    try:
        url = make_url(test_database_url).set(username=role, password=_ROLE_PASSWORD)
        conn = await _connect(url.render_as_string(hide_password=False))
        try:
            for statement in (
                "SELECT 1 FROM admin_settings LIMIT 1",
                "UPDATE admin_settings SET airgapped = true",
                _FULL_SETTINGS_INSERT,
                "SELECT 1 FROM admin_setting_changes LIMIT 1",
                "INSERT INTO admin_setting_changes (setting) VALUES ('airgapped')",
            ):
                with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                    await conn.execute(statement)
        finally:
            await conn.close()
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text(f"DROP ROLE {role}"))


@pytest.mark.asyncio
async def test_the_admin_login_writes_the_row_and_the_history(
    test_database_url: str, _apply_migrations_once: None
) -> None:
    conn = await _connect(login_url(test_database_url, "openscientist_admin", ADMIN_PASSWORD))
    try:
        await conn.execute("DELETE FROM admin_setting_changes")
        await conn.execute("DELETE FROM admin_settings")
        await conn.execute(_FULL_SETTINGS_INSERT)
        assert await conn.fetchval("SELECT count(*) FROM admin_settings") == 1
        await conn.execute(
            "INSERT INTO admin_setting_changes (setting, old_value, new_value) "
            "VALUES ('airgapped', 'false', 'true')"
        )
        assert await conn.fetchval("SELECT count(*) FROM admin_setting_changes") == 1
        await conn.execute("DELETE FROM admin_setting_changes")
        await conn.execute("DELETE FROM admin_settings")
    finally:
        await conn.close()
