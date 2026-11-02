"""Add the reason a queued job waits on, for the budget hold.

A queued job a project limit holds keeps its place in the queue; the column
holds the limit's message while it waits and is cleared when the job starts
or is cancelled. Only the web's app role and the admin role touch the column,
so the agent role's grants stay as they are.

Revision ID: job_queue_reason
Revises: admin_settings
Create Date: 2026-11-01 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "job_queue_reason"
down_revision: str | None = "admin_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("queue_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "queue_reason")
