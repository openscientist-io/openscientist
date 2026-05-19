"""Add guided job template metadata.

Revision ID: add_job_templates
Revises: rename_title_to_rq
Create Date: 2026-05-18 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "add_job_templates"
down_revision: str = "rename_title_to_rq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "template_id",
            sa.String(length=100),
            nullable=True,
            comment="Guided job template identifier, NULL for freeform jobs",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "template_version",
            sa.String(length=40),
            nullable=True,
            comment="Version of the guided job template used at submission time",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "template_inputs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Structured inputs captured for the guided job template",
        ),
    )
    op.create_index(op.f("ix_jobs_template_id"), "jobs", ["template_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_jobs_template_id"), table_name="jobs")
    op.drop_column("jobs", "template_inputs")
    op.drop_column("jobs", "template_version")
    op.drop_column("jobs", "template_id")
