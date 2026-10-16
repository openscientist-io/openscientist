"""Add jobs.is_public, the flag a job owner sets to make a job readable.

This migration only adds the column (default false, so every existing job
stays private). The access rules that act on it come in the next migration,
add_job_public_rls; until then the flag has no effect on who can read a job.

Revision ID: add_job_is_public
Revises: add_version_info
Create Date: 2026-09-28 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "add_job_is_public"
down_revision: str | None = "add_version_info"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Owner has made this job readable by any signed-in user",
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "is_public")
