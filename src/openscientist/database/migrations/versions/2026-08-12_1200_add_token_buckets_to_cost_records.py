"""Add cache and reasoning token columns to cost_records.

Cached reads dominate agentic runs, every turn resending the conversation, so
cost computed from input and output tokens understates reality and per-model
token comparisons are meaningless.

Revision ID: add_token_buckets
Revises: add_pubmed_mirror
Create Date: 2026-08-12 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "add_token_buckets"
down_revision: str | None = "add_pubmed_mirror"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("cache_read_tokens", "Prompt tokens served from cache"),
    ("cache_write_tokens", "Prompt tokens written to cache"),
    ("reasoning_tokens", "Reasoning tokens reported separately from output"),
)


def upgrade() -> None:
    for name, comment in _COLUMNS:
        op.add_column(
            "cost_records",
            sa.Column(
                name,
                sa.Integer(),
                nullable=False,
                server_default="0",
                comment=comment,
            ),
        )


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        op.drop_column("cost_records", name)
