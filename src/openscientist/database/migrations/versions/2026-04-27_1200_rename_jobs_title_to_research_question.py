"""Rename jobs.title to jobs.research_question.

Revision ID: rename_title_to_rq
Revises: add_review_tokens
Create Date: 2026-04-27 12:00:00.000000+00:00

The column has always stored the full research question that drives the
agent prompt, not a short display title. The actual short label lives in
jobs.short_title. Renaming the column to match its real semantics so
``Job.title`` no longer mis-suggests a short label everywhere it is read.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "rename_title_to_rq"
down_revision: str = "add_review_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "jobs",
        "title",
        new_column_name="research_question",
        existing_comment="Research question / job title",
        comment="Research question that drives the agent prompt",
    )


def downgrade() -> None:
    op.alter_column(
        "jobs",
        "research_question",
        new_column_name="title",
        existing_comment="Research question that drives the agent prompt",
        comment="Research question / job title",
    )
