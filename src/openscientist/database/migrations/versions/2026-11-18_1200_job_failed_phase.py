"""Record the phase a failed job failed in, iterations or report.

A job whose report step failed keeps every finding, so an admin may regenerate
its report. Failed jobs before this revision are backfilled from the message
the report step fails with. Both writers of a job's status set the phase, so
both roles may update it.

Revision ID: job_failed_phase
Revises: llm_call_timings
Create Date: 2026-11-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "job_failed_phase"
down_revision: str | None = "llm_call_timings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("failed_phase", sa.String(20), nullable=True))
    op.execute(
        "UPDATE jobs SET failed_phase = CASE"
        " WHEN error_message LIKE 'Report generation failed%' THEN 'report'"
        " ELSE 'iterations' END"
        " WHERE status = 'failed'"
    )
    op.execute("GRANT UPDATE (failed_phase) ON jobs TO openscientist_agent")
    op.execute("GRANT UPDATE (failed_phase) ON jobs TO openscientist_app")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (failed_phase) ON jobs FROM openscientist_app")
    op.execute("REVOKE UPDATE (failed_phase) ON jobs FROM openscientist_agent")
    op.drop_column("jobs", "failed_phase")
