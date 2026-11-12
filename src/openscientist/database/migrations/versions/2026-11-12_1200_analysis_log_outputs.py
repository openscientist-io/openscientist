"""Keep raw action output in an owner and share recipient table.

Revision ID: analysis_log_outputs
Revises: durable_cost_ledger
Create Date: 2026-11-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "analysis_log_outputs"
down_revision: str | None = "durable_cost_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNER = """
    current_setting('app.current_user_id', TRUE) IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM jobs
        WHERE jobs.id = analysis_log_outputs.job_id
        AND jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
    )
"""
_SHARED = """
    current_setting('app.current_user_id', TRUE) IS NOT NULL
    AND EXISTS (
        SELECT 1 FROM jobs
        JOIN job_shares ON job_shares.job_id = jobs.id
        WHERE jobs.id = analysis_log_outputs.job_id
        AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
        {permission}
    )
"""


def upgrade() -> None:
    op.create_unique_constraint("uq_analysis_log_id_job_id", "analysis_log", ["id", "job_id"])
    # The output references its log together with that log's job, so a row can
    # never pair one job's id with another job's log.
    op.create_table(
        "analysis_log_outputs",
        sa.Column("analysis_log_id", sa.UUID(), primary_key=True),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_log_id", "job_id"],
            ["analysis_log.id", "analysis_log.job_id"],
            ondelete="CASCADE",
            name="analysis_log_outputs_log_fkey",
        ),
    )
    op.create_index("ix_analysis_log_outputs_job_id", "analysis_log_outputs", ["job_id"])
    op.execute("ALTER TABLE analysis_log NO FORCE ROW LEVEL SECURITY")
    op.execute("""
        INSERT INTO analysis_log_outputs (analysis_log_id, job_id, output)
        SELECT id, job_id, output_data->>'output' FROM analysis_log
        WHERE output_data->>'output' IS NOT NULL AND output_data->>'output' <> ''
    """)
    op.execute("ALTER TABLE analysis_log FORCE ROW LEVEL SECURITY")
    op.drop_column("analysis_log", "output_data")
    op.execute("ALTER TABLE analysis_log_outputs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE analysis_log_outputs FORCE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON analysis_log_outputs TO openscientist_app")
    op.execute(
        f"CREATE POLICY analysis_log_outputs_all_owner ON analysis_log_outputs "
        f"FOR ALL TO openscientist_app USING ({_OWNER})"
    )
    shared = _SHARED.format(permission="")
    edit = _SHARED.format(permission="AND job_shares.permission_level = 'edit'")
    op.execute(
        f"CREATE POLICY analysis_log_outputs_select_shared ON analysis_log_outputs "
        f"FOR SELECT TO openscientist_app USING ({shared})"
    )
    for name, command, clause in (
        ("modify", "INSERT", "WITH CHECK"),
        ("update", "UPDATE", "USING"),
        ("delete", "DELETE", "USING"),
    ):
        op.execute(
            f"CREATE POLICY analysis_log_outputs_{name}_shared_edit ON analysis_log_outputs "
            f"FOR {command} TO openscientist_app {clause} ({edit})"
        )
    op.execute("GRANT SELECT, INSERT ON analysis_log_outputs TO openscientist_agent")
    for name, mode in (("agent_job", "PERMISSIVE"), ("agent_only_own_job", "RESTRICTIVE")):
        op.execute(
            f"CREATE POLICY analysis_log_outputs_{name} ON analysis_log_outputs AS {mode} "
            "FOR ALL TO openscientist_agent "
            "USING (job_id = agent_job_id()) WITH CHECK (job_id = agent_job_id())"
        )


def downgrade() -> None:
    op.add_column(
        "analysis_log",
        sa.Column(
            "output_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Output/result data from this step",
        ),
    )
    op.execute("ALTER TABLE analysis_log NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE analysis_log_outputs NO FORCE ROW LEVEL SECURITY")
    op.execute("""
        UPDATE analysis_log SET output_data = jsonb_build_object('output', outputs.output)
        FROM analysis_log_outputs AS outputs WHERE analysis_log.id = outputs.analysis_log_id
    """)
    op.execute("ALTER TABLE analysis_log FORCE ROW LEVEL SECURITY")
    op.drop_table("analysis_log_outputs")
    op.drop_constraint("uq_analysis_log_id_job_id", "analysis_log", type_="unique")
