"""Add the per-job MARDUK rare-disease mode flag.

Revision ID: add_marduk_flag
Revises: rename_title_to_rq
Create Date: 2026-07-08 12:00:00.000000+00:00

Adds ``jobs.marduk_enabled`` — the per-job toggle for MARDUK rare-disease mode
(Monarch Initiative tools). The persistent-memory table is added in a later
migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_marduk_flag"
down_revision: str = "rename_title_to_rq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the marduk_enabled column."""
    op.add_column(
        "jobs",
        sa.Column(
            "marduk_enabled",
            sa.Boolean(),
            server_default="false",
            nullable=False,
            comment="Whether MARDUK rare-disease mode (Monarch tools + memory) is enabled",
        ),
    )


def downgrade() -> None:
    """Drop the marduk_enabled column."""
    op.drop_column("jobs", "marduk_enabled")
