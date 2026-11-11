"""Retain attributed expenditure independently of jobs.

Revision ID: durable_cost_ledger
Revises: drop_job_formatted_prompt
Create Date: 2026-11-11 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "durable_cost_ledger"
down_revision: str | None = "drop_job_formatted_prompt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER = """
    EXISTS (
        SELECT 1 FROM jobs
        WHERE jobs.id = cost_records.job_id
        AND jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
    )
"""


def upgrade() -> None:
    op.add_column("cost_records", sa.Column("user_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "cost_records_user_id_fkey",
        "cost_records",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_cost_records_user_id", "cost_records", ["user_id"])
    op.execute("ALTER TABLE cost_records NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE cost_records SET user_id = jobs.owner_id FROM jobs WHERE jobs.id = cost_records.job_id"
    )
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cost_records FORCE ROW LEVEL SECURITY")
    op.drop_constraint("cost_records_job_id_fkey", "cost_records", type_="foreignkey")
    op.alter_column("cost_records", "job_id", existing_type=sa.UUID(), nullable=True)
    op.create_foreign_key(
        "cost_records_job_id_fkey",
        "cost_records",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("DROP POLICY cost_records_all_owner ON cost_records")
    op.execute(
        f"CREATE POLICY cost_records_select_owner ON cost_records FOR SELECT TO openscientist_app USING (user_id::text = current_setting('app.current_user_id', TRUE) OR {_OWNER})"
    )
    op.execute(
        f"CREATE POLICY cost_records_insert_owner ON cost_records FOR INSERT TO openscientist_app WITH CHECK ({_OWNER})"
    )
    op.execute("REVOKE UPDATE, DELETE ON cost_records FROM openscientist_app")


def downgrade() -> None:
    op.execute("ALTER TABLE cost_records NO FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM cost_records WHERE job_id IS NULL) THEN
                RAISE EXCEPTION 'Cannot downgrade while expenditure from deleted jobs is retained';
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE cost_records FORCE ROW LEVEL SECURITY")
    op.execute("GRANT UPDATE, DELETE ON cost_records TO openscientist_app")
    op.execute("DROP POLICY cost_records_insert_owner ON cost_records")
    op.execute("DROP POLICY cost_records_select_owner ON cost_records")
    op.execute(
        f"CREATE POLICY cost_records_all_owner ON cost_records FOR ALL TO openscientist_app USING ({_OWNER})"
    )
    op.drop_constraint("cost_records_job_id_fkey", "cost_records", type_="foreignkey")
    op.alter_column("cost_records", "job_id", existing_type=sa.UUID(), nullable=False)
    op.create_foreign_key(
        "cost_records_job_id_fkey",
        "cost_records",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_index("ix_cost_records_user_id", table_name="cost_records")
    op.drop_constraint("cost_records_user_id_fkey", "cost_records", type_="foreignkey")
    op.drop_column("cost_records", "user_id")
