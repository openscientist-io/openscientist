"""Add approved_at timestamp to users.

Revision ID: add_approved_at
Revises: add_version_info
Create Date: 2026-10-15 12:00:00

Lets the admin Users table sort by when a user was approved. Previously only
a boolean is_approved flag existed, with no record of when approval happened.

Backfills already-approved users with their updated_at as a best-effort
approximation (the moment the is_approved flag was last flipped true is
usually also the last time the row was updated), rather than leaving every
user approved before this migration permanently unsortable against users
approved after it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "add_approved_at"
down_revision: str | None = "add_version_info"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When an administrator approved this user (None if pending or never approved)",
        ),
    )
    # Best-effort backfill: updated_at is not guaranteed to be the exact
    # approval moment (any later edit to the row would also bump it), but
    # it's a far better default than leaving every already-approved user
    # permanently stuck at "—" and unsortable against future approvals.
    op.execute(
        "UPDATE users SET approved_at = updated_at WHERE is_approved = true AND approved_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("users", "approved_at")
