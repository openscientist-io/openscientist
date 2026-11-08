"""Drop the jobs column that held the model-formatted research question, with
the container role's write grant.

Revision ID: drop_job_formatted_prompt
Revises: job_formatted_prompt
Create Date: 2026-11-08 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "drop_job_formatted_prompt"
down_revision: str | None = "job_formatted_prompt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("jobs", "formatted_prompt")


def downgrade() -> None:
    op.add_column("jobs", sa.Column("formatted_prompt", sa.Text(), nullable=True))
    op.execute("GRANT UPDATE (formatted_prompt) ON jobs TO openscientist_agent")
