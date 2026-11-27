"""The migration adding the MCP server catalog follows the current head and
upgrades and downgrades cleanly."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

_ROOT = Path(__file__).parent.parent
_MIGRATION = _ROOT / "src/openscientist/database/migrations/versions/2026-11-27_1200_mcp_servers.py"

_ROLES = ("openscientist_app", "openscientist_agent", "openscientist_web", "openscientist_admin")
_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mcp_servers", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_catalog_is_the_single_head_after_admins_grant_admins() -> None:
    scripts = ScriptDirectory.from_config(Config(str(_ROOT / "alembic.ini")))
    assert scripts.get_heads() == ["mcp_servers"]
    revision = scripts.get_revision("mcp_servers")
    assert revision is not None
    assert revision.down_revision == "admins_grant_admins"


def _table_exists(connection: Connection) -> bool:
    return bool(
        connection.execute(text("SELECT to_regclass('public.mcp_servers') IS NOT NULL")).scalar()
    )


def _privileges(connection: Connection) -> dict[str, set[str]]:
    """Which privileges each role and PUBLIC hold on the table."""
    held: dict[str, set[str]] = {}
    for role in _ROLES:
        held[role] = {
            privilege
            for privilege in _PRIVILEGES
            if connection.execute(
                text("SELECT has_table_privilege(:role, 'public.mcp_servers', :privilege)"),
                {"role": role, "privilege": privilege},
            ).scalar()
        }
    # Grantee 0 in an ACL is PUBLIC.
    held["PUBLIC"] = set(
        connection.execute(
            text(
                "SELECT acl.privilege_type FROM pg_class, aclexplode(relacl) AS acl"
                " WHERE pg_class.oid = 'public.mcp_servers'::regclass AND acl.grantee = 0"
            )
        ).scalars()
    )
    return held


def _constraints(connection: Connection) -> set[str]:
    return set(
        connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'public.mcp_servers'::regclass"
            )
        ).scalars()
    )


@pytest.mark.asyncio
async def test_mcp_servers_upgrades_and_downgrades_cleanly(test_engine: AsyncEngine) -> None:
    module = _migration()

    def run(connection: Connection) -> None:
        # DDL is transactional in Postgres, so rolling back keeps the shared schema.
        transaction = connection.begin()
        try:
            # As the migrations run, so the owner's default privileges apply.
            connection.execute(text("SET LOCAL ROLE openscientist_owner"))
            with Operations.context(MigrationContext.configure(connection)):
                module.downgrade()
                assert not _table_exists(connection)
                module.upgrade()
            assert _table_exists(connection)
            rls = connection.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
                    " WHERE oid = 'public.mcp_servers'::regclass"
                )
            ).one()
            assert tuple(rls) == (True, True)
            assert _privileges(connection) == {
                "openscientist_app": set(),
                "openscientist_agent": set(),
                "openscientist_web": set(),
                "openscientist_admin": {"SELECT", "INSERT"},
                "PUBLIC": set(),
            }
            assert {
                "uq_mcp_servers_slug",
                "ck_mcp_servers_slug",
                "ck_mcp_servers_display_name",
                "ck_mcp_servers_url",
                "ck_mcp_servers_inactive",
            } <= _constraints(connection)
            policies = connection.execute(
                text("SELECT count(*) FROM pg_policies WHERE tablename = 'mcp_servers'")
            ).scalar()
            assert policies == 0
        finally:
            transaction.rollback()

    async with test_engine.connect() as conn:
        await conn.run_sync(run)
