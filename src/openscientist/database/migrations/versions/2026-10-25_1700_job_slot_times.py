"""Record when each job takes and gives back a slot, for the balancing scheduler.

Revision ID: job_slot_times
Revises: job_start_permission
Create Date: 2026-10-25 17:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "job_slot_times"
down_revision: str | None = "job_start_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True))
    # updated_at is a lower bound on when a job still holding a slot started.
    op.execute(
        "UPDATE jobs SET started_at = updated_at WHERE status IN ('running', 'awaiting_feedback')"
    )
    op.execute(
        "UPDATE jobs SET ended_at = updated_at WHERE status IN ('completed', 'failed', 'cancelled')"
    )
    # The container's final status write also records the end of the run.
    op.execute("GRANT UPDATE (ended_at) ON jobs TO openscientist_agent")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (ended_at) ON jobs FROM openscientist_agent")
    op.drop_column("jobs", "ended_at")
    op.drop_column("jobs", "started_at")
