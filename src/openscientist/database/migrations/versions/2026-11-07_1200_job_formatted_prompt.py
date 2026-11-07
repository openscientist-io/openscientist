"""Add the display version of the research question the model formats at job start.

The agent container's first turn writes an HTML fragment of the question for
the job page, so the agent role may update the column for its own job. The web
renders it through a sanitizer and never writes it.

Revision ID: job_formatted_prompt
Revises: orphaned_jobs_admin_only
Create Date: 2026-11-07 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "job_formatted_prompt"
down_revision: str | None = "orphaned_jobs_admin_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("formatted_prompt", sa.Text(), nullable=True))
    op.execute("GRANT UPDATE (formatted_prompt) ON jobs TO openscientist_agent")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (formatted_prompt) ON jobs FROM openscientist_agent")
    op.drop_column("jobs", "formatted_prompt")
