"""Add the note a completed job ends with when a budget limit stops it.

The job cap, a project limit, or a check the web could not answer in time all
end the job completed with the note naming the limit, or saying the budget
could not be checked and why. The container's final status write carries it,
so the agent role may update it for its own job, the way it may set
error_message.

Revision ID: job_stop_note
Revises: job_queue_reason
Create Date: 2026-11-01 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "job_stop_note"
down_revision: str | None = "job_queue_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("stop_note", sa.Text(), nullable=True))
    # The container's final status write carries the note.
    op.execute("GRANT UPDATE (stop_note) ON jobs TO openscientist_agent")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (stop_note) ON jobs FROM openscientist_agent")
    op.drop_column("jobs", "stop_note")
