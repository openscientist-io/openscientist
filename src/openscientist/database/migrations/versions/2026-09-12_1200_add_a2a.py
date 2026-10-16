"""Add A2A admission control and owner-scoped task associations.

Revision ID: add_a2a
Revises: add_version_info
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "add_a2a"
down_revision: str | None = "add_version_info"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create settings and tasks with RLS matching API ownership rules."""
    op.create_table(
        "a2a_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.CheckConstraint("id = 1", name="a2a_settings_singleton"),
    )
    op.execute("INSERT INTO a2a_settings (id, enabled) VALUES (1, true)")
    op.create_table(
        "a2a_tasks",
        sa.Column("job_id", sa.UUID(), primary_key=True),
        sa.Column("message", postgresql.JSONB(), nullable=False),
        sa.Column("final_task", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
    )
    for table in ("a2a_settings", "a2a_tasks"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"GRANT ALL ON {table} TO openscientist_app, openscientist_admin")
    op.execute("CREATE POLICY a2a_settings_read ON a2a_settings FOR SELECT USING (true)")
    op.execute(
        "CREATE POLICY a2a_settings_write ON a2a_settings FOR UPDATE "
        "USING (current_user_is_admin()) WITH CHECK (current_user_is_admin())"
    )
    op.execute(
        """CREATE POLICY a2a_tasks_owner ON a2a_tasks FOR ALL
        USING (EXISTS (SELECT 1 FROM jobs WHERE jobs.id = a2a_tasks.job_id
            AND jobs.owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid))
        WITH CHECK (EXISTS (SELECT 1 FROM jobs WHERE jobs.id = a2a_tasks.job_id
            AND jobs.owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid))"""
    )


def downgrade() -> None:
    """Remove protocol metadata; ordinary jobs are preserved."""
    op.drop_table("a2a_tasks")
    op.drop_table("a2a_settings")
