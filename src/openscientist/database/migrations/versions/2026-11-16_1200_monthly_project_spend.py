"""Limit project spend per calendar month instead of over all time.

The limit keeps its value, so a deployment's all-time cap becomes the same cap
for each calendar month (UTC).

Revision ID: monthly_project_spend
Revises: revoke_app_truncate_again
Create Date: 2026-11-16 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "monthly_project_spend"
down_revision: str | None = "revoke_app_truncate_again"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE admin_settings "
        "RENAME COLUMN max_project_spend_total_usd TO max_project_spend_month_usd"
    )
    op.execute(
        "ALTER TABLE admin_settings RENAME CONSTRAINT "
        "ck_admin_settings_max_project_spend_total_usd "
        "TO ck_admin_settings_max_project_spend_month_usd"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE admin_settings RENAME CONSTRAINT "
        "ck_admin_settings_max_project_spend_month_usd "
        "TO ck_admin_settings_max_project_spend_total_usd"
    )
    op.execute(
        "ALTER TABLE admin_settings "
        "RENAME COLUMN max_project_spend_month_usd TO max_project_spend_total_usd"
    )
