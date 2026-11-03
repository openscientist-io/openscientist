"""Restrict the orphaned-job select policy to signed-in admins.

Deleting a user leaves their jobs with owner_id NULL, and the admin page lists
and reassigns those rows through the RLS-bypassing admin session. The policy
that admitted the rows had no owner, share, or public test, so any signed-in
user who knew an id could read the job, its artifacts and its report. The
policy admits an ownerless job only through current_user_is_admin, which
reads app.current_user_id like the other user-scoped policies.

Revision ID: orphaned_jobs_admin_only
Revises: narrow_database_grants
Create Date: 2026-11-03 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "orphaned_jobs_admin_only"
down_revision: str | None = "narrow_database_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER POLICY jobs_select_orphaned ON jobs "
        "USING (owner_id IS NULL AND current_user_is_admin())"
    )


def downgrade() -> None:
    op.execute("ALTER POLICY jobs_select_orphaned ON jobs USING (owner_id IS NULL)")
