"""Only approved users and administrators may create jobs.

Revision ID: job_start_permission
Revises: definer_search_path
Create Date: 2026-10-25 16:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "job_start_permission"
down_revision: str | None = "definer_search_path"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SECURITY DEFINER lets the check read users and administrators past their
    # own RLS. The fixed search_path stops it resolving shadowing objects.
    op.execute(
        """
        CREATE FUNCTION user_can_start_jobs(check_user_id uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM public.users WHERE id = check_user_id AND is_approved
            ) OR public.is_admin(check_user_id)
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION user_can_start_jobs(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION user_can_start_jobs(uuid) TO openscientist_app")
    op.execute(
        """
        CREATE POLICY jobs_insert_requires_start_permission ON jobs
            AS RESTRICTIVE FOR INSERT TO openscientist_app
            WITH CHECK (user_can_start_jobs(owner_id))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY jobs_insert_requires_start_permission ON jobs")
    op.execute("DROP FUNCTION user_can_start_jobs(uuid)")
