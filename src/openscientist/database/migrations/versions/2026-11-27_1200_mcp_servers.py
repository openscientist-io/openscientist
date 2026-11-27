"""Add mcp_servers, the admins' catalog of remote MCP servers.

A registration is catalog data only. A check keeps every row inactive, and no
job reads the table. Only the admin login may read the catalog or add to it,
and it may neither change nor delete a registration. Row-level security is
enabled and forced with no policy, so a later grant to the app role still
shows it nothing.

Revision ID: mcp_servers
Revises: admins_grant_admins
Create Date: 2026-11-27 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "mcp_servers"
down_revision: str | None = "admins_grant_admins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE mcp_servers (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            slug text NOT NULL
                CONSTRAINT uq_mcp_servers_slug UNIQUE
                CONSTRAINT ck_mcp_servers_slug CHECK (slug ~ '^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'),
            display_name text NOT NULL
                CONSTRAINT ck_mcp_servers_display_name CHECK (
                    display_name = btrim(display_name)
                    AND char_length(display_name) BETWEEN 1 AND 200
                ),
            url text NOT NULL
                CONSTRAINT ck_mcp_servers_url CHECK (
                    url ~ '^https?://[^\s/?#@]+([/?#][^\s]*)?$' AND char_length(url) <= 2048
                ),
            is_active boolean NOT NULL DEFAULT false
                CONSTRAINT ck_mcp_servers_inactive CHECK (NOT is_active),
            created_by uuid REFERENCES users (id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE mcp_servers IS "
        "'Remote MCP servers the admins registered, all inactive and unused by jobs'"
    )
    op.execute("COMMENT ON COLUMN mcp_servers.slug IS 'Unique identifier the admins chose'")
    op.execute("COMMENT ON COLUMN mcp_servers.display_name IS 'Name shown to admins'")
    op.execute("COMMENT ON COLUMN mcp_servers.url IS 'HTTP(S) endpoint, never called'")
    op.execute(
        "COMMENT ON COLUMN mcp_servers.is_active IS 'Always false; no job uses a registered server'"
    )
    op.execute(
        "COMMENT ON COLUMN mcp_servers.created_by IS 'Admin who registered it, NULL once deleted'"
    )
    op.execute("ALTER TABLE mcp_servers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_servers FORCE ROW LEVEL SECURITY")
    # The default privileges hand every new table to the app and admin roles.
    op.execute(
        "REVOKE ALL ON mcp_servers FROM openscientist_app, openscientist_agent, "
        "openscientist_web, openscientist_admin, PUBLIC"
    )
    op.execute("GRANT SELECT, INSERT ON mcp_servers TO openscientist_admin")


def downgrade() -> None:
    op.execute("DROP TABLE mcp_servers")
