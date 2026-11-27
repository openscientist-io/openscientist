"""The MCP server catalog grants. Only the admin login reads the catalog or adds
to it, and no login may activate, change or delete a registration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio
from sqlalchemy import delete, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import McpServer
from tests.db_logins import ADMIN_PASSWORD, WEB_PASSWORD, login_url

_ROLE_PASSWORD = "mcp-servers-role-test-password"

_INSERT = (
    "INSERT INTO mcp_servers (slug, display_name, url) "
    "VALUES ('role-test', 'Role test', 'https://mcp.example.org/')"
)
_EVERY_STATEMENT = (
    "SELECT 1 FROM mcp_servers LIMIT 1",
    _INSERT,
    "UPDATE mcp_servers SET is_active = false",
    "DELETE FROM mcp_servers",
    "TRUNCATE mcp_servers",
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


async def _assert_refused(conn: asyncpg.Connection, statements: tuple[str, ...]) -> None:
    for statement in statements:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(statement)


@pytest_asyncio.fixture
async def catalog(test_engine: AsyncEngine, _apply_migrations_once: None) -> AsyncIterator[None]:
    """One registered server, removed again afterwards."""
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(McpServer))
        db.add(McpServer(slug="existing", display_name="Existing", url="https://a.example.org/"))
        await db.commit()
    yield
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(McpServer))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("as_app_role", [False, True], ids=["web_login", "app_role"])
async def test_the_web_login_and_the_app_role_cannot_touch_the_catalog(
    test_database_url: str, catalog: None, as_app_role: bool
) -> None:
    conn = await _connect(login_url(test_database_url, "openscientist_web", WEB_PASSWORD))
    try:
        if as_app_role:
            await conn.execute("SET ROLE openscientist_app")
        await _assert_refused(conn, _EVERY_STATEMENT)
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["openscientist_agent", None], ids=["agent", "public"])
async def test_a_job_login_and_a_login_with_no_role_cannot_touch_the_catalog(
    test_database_url: str, test_engine: AsyncEngine, catalog: None, membership: str | None
) -> None:
    role = f"mcp_servers_{uuid4().hex}"
    in_role = f" IN ROLE {membership}" if membership else ""
    async with test_engine.begin() as conn:
        await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{_ROLE_PASSWORD}'{in_role}"))
    try:
        url = make_url(test_database_url).set(username=role, password=_ROLE_PASSWORD)
        conn = await _connect(url.render_as_string(hide_password=False))
        try:
            await _assert_refused(conn, _EVERY_STATEMENT)
        finally:
            await conn.close()
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text(f"DROP ROLE {role}"))


@pytest.mark.asyncio
async def test_the_admin_login_reads_and_adds_but_cannot_change_or_delete(
    test_database_url: str, catalog: None
) -> None:
    conn = await _connect(login_url(test_database_url, "openscientist_admin", ADMIN_PASSWORD))
    try:
        assert await conn.fetchval("SELECT count(*) FROM mcp_servers") == 1
        await conn.execute(_INSERT)
        assert await conn.fetchval("SELECT count(*) FROM mcp_servers") == 2
        await _assert_refused(
            conn,
            (
                "UPDATE mcp_servers SET is_active = true",
                "UPDATE mcp_servers SET url = 'https://b.example.org/'",
                "DELETE FROM mcp_servers",
                "TRUNCATE mcp_servers",
            ),
        )
        assert await conn.fetchval("SELECT count(*) FROM mcp_servers WHERE is_active") == 0
    finally:
        await conn.close()
