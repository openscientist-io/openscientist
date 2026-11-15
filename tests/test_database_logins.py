"""The logins ``bootstrap.sql`` sets up. The owner owns the schema, and the web
login can do nothing beyond switching to ``openscientist_app`` and running agent
logins."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.database.login_guard import database_login_problem, leaked_database_secrets
from tests.db_logins import (
    ADMIN_PASSWORD,
    BOOTSTRAP_ENV,
    OWNER_PASSWORD,
    WEB_PASSWORD,
    login_url,
)

Bootstrap = Callable[[Mapping[str, str]], tuple[int, str]]

_LOGINS = (
    ("openscientist_owner", "POSTGRES_OWNER_PASSWORD", OWNER_PASSWORD),
    ("openscientist_web", "POSTGRES_WEB_PASSWORD", WEB_PASSWORD),
    ("openscientist_admin", "POSTGRES_ADMIN_PASSWORD", ADMIN_PASSWORD),
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


async def _logs_in(database_url: str) -> bool:
    try:
        conn = await _connect(database_url)
    except asyncpg.exceptions.InvalidPasswordError:
        return False
    await conn.close()
    return True


@pytest.mark.asyncio
async def test_bootstrap_hands_the_superusers_objects_to_the_owner(
    bootstrap: Bootstrap, test_engine: AsyncEngine
) -> None:
    """An existing deployment's schema belongs to the image superuser until the
    bootstrap runs. Extension members stay with the extension."""
    async with test_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE legacy_table (id serial PRIMARY KEY)"))
        await conn.execute(text("CREATE SEQUENCE legacy_sequence"))
        await conn.execute(text("CREATE VIEW legacy_view AS SELECT 1 AS one"))
        await conn.execute(
            text("CREATE FUNCTION legacy_function() RETURNS int LANGUAGE sql AS 'SELECT 1'")
        )
        await conn.execute(text("CREATE TYPE legacy_type AS ENUM ('a')"))
    try:
        exit_code, output = bootstrap(BOOTSTRAP_ENV)
        assert exit_code == 0, output
        async with test_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT relname, pg_get_userbyid(relowner) FROM pg_class
                        WHERE relname IN ('legacy_table', 'legacy_table_id_seq',
                                          'legacy_sequence', 'legacy_view')
                        UNION ALL
                        SELECT proname, pg_get_userbyid(proowner) FROM pg_proc
                        WHERE proname IN ('legacy_function', 'similarity')
                        UNION ALL
                        SELECT typname, pg_get_userbyid(typowner) FROM pg_type
                        WHERE typname = 'legacy_type'
                        """
                    )
                )
            ).all()
            owners: dict[str, str] = {name: owner for name, owner in rows}
            superuser = await conn.scalar(text("SELECT current_user"))
    finally:
        async with test_engine.begin() as conn:
            await conn.execute(text("DROP VIEW legacy_view"))
            await conn.execute(text("DROP TABLE legacy_table"))
            await conn.execute(text("DROP SEQUENCE legacy_sequence"))
            await conn.execute(text("DROP FUNCTION legacy_function()"))
            await conn.execute(text("DROP TYPE legacy_type"))
    assert owners == {
        "legacy_table": "openscientist_owner",
        "legacy_table_id_seq": "openscientist_owner",
        "legacy_sequence": "openscientist_owner",
        "legacy_view": "openscientist_owner",
        "legacy_function": "openscientist_owner",
        "legacy_type": "openscientist_owner",
        "similarity": superuser,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "variable", "password"), _LOGINS)
async def test_bootstrap_sets_each_password_on_every_run(
    bootstrap: Bootstrap,
    test_database_url: str,
    _apply_migrations_once: None,
    role: str,
    variable: str,
    password: str,
) -> None:
    exit_code, output = bootstrap({**BOOTSTRAP_ENV, variable: "rotated-password"})
    try:
        assert exit_code == 0, output
        assert await _logs_in(login_url(test_database_url, role, "rotated-password"))
        assert not await _logs_in(login_url(test_database_url, role, password))
    finally:
        assert bootstrap(BOOTSTRAP_ENV)[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("variable", [variable for _, variable, _ in _LOGINS])
@pytest.mark.parametrize("value", [None, ""])
async def test_bootstrap_refuses_a_missing_password_before_changing_anything(
    bootstrap: Bootstrap,
    test_database_url: str,
    _apply_migrations_once: None,
    variable: str,
    value: str | None,
) -> None:
    env = {name: "changed-password" for name in BOOTSTRAP_ENV if name != variable}
    if value is not None:
        env[variable] = value
    exit_code, output = bootstrap(env)
    assert exit_code != 0
    assert variable in output
    for role, _, password in _LOGINS:
        assert await _logs_in(login_url(test_database_url, role, password)), role


@pytest.mark.asyncio
async def test_the_migrated_schema_belongs_to_the_owner(test_engine: AsyncEngine) -> None:
    """Alembic runs as the owner, so every relation and function of ours is its."""
    async with test_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    """
                    SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c
                    WHERE c.relnamespace = 'public'::regnamespace
                      AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'i')
                      AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = c.oid
                                      AND d.classid = 'pg_class'::regclass AND d.deptype = 'e')
                    UNION ALL
                    SELECT p.proname, pg_get_userbyid(p.proowner) FROM pg_proc p
                    WHERE p.pronamespace = 'public'::regnamespace
                      AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.objid = p.oid
                                      AND d.classid = 'pg_proc'::regclass AND d.deptype = 'e')
                    """
                )
            )
        ).all()
        database_owner = await conn.scalar(
            text(
                "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()"
            )
        )
    owners: dict[str, str] = {name: owner for name, owner in rows}
    assert {"jobs", "agent_job_roles", "agent_job_id"} <= owners.keys()
    assert set(owners.values()) == {"openscientist_owner"}
    assert database_owner == "openscientist_owner"


@pytest.mark.asyncio
async def test_tables_the_owner_creates_reach_the_app_and_admin_roles(
    test_database_url: str, test_engine: AsyncEngine
) -> None:
    conn = await _connect(login_url(test_database_url, "openscientist_owner", OWNER_PASSWORD))
    try:
        await conn.execute("CREATE TABLE owner_made (id int)")
    finally:
        await conn.close()
    try:
        async with test_engine.connect() as engine_conn:
            granted = (
                await engine_conn.execute(
                    text(
                        "SELECT has_table_privilege('openscientist_app', 'owner_made', "
                        "'SELECT, INSERT, UPDATE, DELETE'), "
                        "has_table_privilege('openscientist_admin', 'owner_made', "
                        "'SELECT, INSERT, UPDATE, DELETE')"
                    )
                )
            ).one()
    finally:
        async with test_engine.begin() as engine_conn:
            await engine_conn.execute(text("DROP TABLE owner_made"))
    assert tuple(granted) == (True, True)


@pytest.mark.asyncio
async def test_a_rerun_bootstrap_keeps_truncate_from_the_app_role(
    bootstrap: Bootstrap, test_database_url: str, test_engine: AsyncEngine
) -> None:
    """Row-level security does not apply to ``TRUNCATE``, so a table the owner
    creates after the bootstrap runs again stays out of the app role's reach."""
    exit_code, output = bootstrap(BOOTSTRAP_ENV)
    assert exit_code == 0, output
    owner_connection = await _connect(
        login_url(test_database_url, "openscientist_owner", OWNER_PASSWORD)
    )
    try:
        await owner_connection.execute("CREATE TABLE owner_made (id int)")
    finally:
        await owner_connection.close()
    web_connection = await _connect(login_url(test_database_url, "openscientist_web", WEB_PASSWORD))
    try:
        await web_connection.execute("SET ROLE openscientist_app")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await web_connection.execute("TRUNCATE owner_made")
    finally:
        await web_connection.close()
        async with test_engine.begin() as engine_connection:
            await engine_connection.execute(text("DROP TABLE owner_made"))


@pytest.mark.asyncio
async def test_the_web_login_reads_no_table_by_itself(
    test_database_url: str, _apply_migrations_once: None
) -> None:
    conn = await _connect(login_url(test_database_url, "openscientist_web", WEB_PASSWORD))
    try:
        attributes = await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        assert tuple(attributes) == (False, False)
        for table in ("jobs", "users", "api_keys", "sessions", "findings"):
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.fetch(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
        await conn.execute("SET ROLE openscientist_app")
        await conn.execute("RESET ROLE")
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "SET ROLE openscientist_admin",
        "SET ROLE openscientist_owner",
        "SET ROLE openscientist_agent",
        "CREATE ROLE web_made_bypass BYPASSRLS",
        "CREATE ROLE web_made_app IN ROLE openscientist_app",
        "ALTER ROLE openscientist_admin PASSWORD 'taken'",
        "GRANT openscientist_admin TO openscientist_web",
        "ALTER ROLE openscientist_web BYPASSRLS",
    ],
)
async def test_the_web_login_cannot_widen_its_rights(
    test_database_url: str, _apply_migrations_once: None, statement: str
) -> None:
    conn = await _connect(login_url(test_database_url, "openscientist_web", WEB_PASSWORD))
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_guard_accepts_only_the_web_login(
    test_database_url: str, _apply_migrations_once: None
) -> None:
    superuser = await database_login_problem(test_database_url)
    admin = await database_login_problem(
        login_url(test_database_url, "openscientist_admin", ADMIN_PASSWORD)
    )
    web = await database_login_problem(
        login_url(test_database_url, "openscientist_web", WEB_PASSWORD)
    )
    assert superuser is not None
    assert "superuser" in superuser
    assert admin is not None
    assert "BYPASSRLS" in admin
    assert web is None


def test_the_guard_names_database_secrets_that_reached_the_web() -> None:
    environ = {
        "POSTGRES_PASSWORD": "superuser-password",
        "POSTGRES_OWNER_PASSWORD": "",
        "POSTGRES_WEB_PASSWORD": "web-password",
        "POSTGRES_ADMIN_PASSWORD": "admin-password",
    }
    assert leaked_database_secrets(environ) == ["POSTGRES_PASSWORD"]
    assert leaked_database_secrets({**environ, "POSTGRES_OWNER_PASSWORD": "owner"}) == [
        "POSTGRES_PASSWORD",
        "POSTGRES_OWNER_PASSWORD",
    ]
