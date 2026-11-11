"""Store session cookie hashes separately from row identifiers.

Revision ID: session_token_hash
Revises: drop_job_formatted_prompt
Create Date: 2026-11-11 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "session_token_hash"
down_revision: str | None = "drop_job_formatted_prompt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("TRUNCATE sessions")
    op.add_column("sessions", sa.Column("token_hash", sa.String(64), nullable=False))
    op.create_unique_constraint("uq_sessions_token_hash", "sessions", ["token_hash"])


def downgrade() -> None:
    op.execute("TRUNCATE sessions")
    op.drop_constraint("uq_sessions_token_hash", "sessions", type_="unique")
    op.drop_column("sessions", "token_hash")
