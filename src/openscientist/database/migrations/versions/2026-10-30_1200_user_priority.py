"""Mark users whose queued jobs start ahead of everyone else's.

Revision ID: user_priority
Revises: job_slot_times
Create Date: 2026-10-30 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "user_priority"
down_revision: str | None = "job_slot_times"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only admins set it. The app role's UPDATE on users covers the notification
    # columns alone, so a user session cannot set this one.
    op.add_column(
        "users",
        sa.Column(
            "is_priority",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Whether this user's queued jobs start ahead of other users' jobs",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_priority")
