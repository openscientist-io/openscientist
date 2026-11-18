"""Control job cost visibility for regular users."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "job_cost_visibility"
down_revision: str | None = "monthly_project_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "admin_settings",
        sa.Column("show_job_costs", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("admin_settings", "show_job_costs")
