"""Cap the iterations a job may ask for with an admin setting.

The row predates the cap, so the migration gives it the 20 a fresh install
seeds. From then on the startup seed supplies the value, as for every other
column of the row.

Revision ID: max_iterations_cap
Revises: user_feedback
Create Date: 2026-11-20 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "max_iterations_cap"
down_revision: str | None = "user_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE admin_settings ADD COLUMN max_iterations integer NOT NULL DEFAULT 20 "
        "CONSTRAINT ck_admin_settings_max_iterations CHECK (max_iterations > 0)"
    )
    op.execute("ALTER TABLE admin_settings ALTER COLUMN max_iterations DROP DEFAULT")


def downgrade() -> None:
    op.execute("ALTER TABLE admin_settings DROP COLUMN max_iterations")
