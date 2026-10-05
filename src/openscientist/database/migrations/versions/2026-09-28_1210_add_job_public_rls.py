"""Row-level security for public jobs: who can read a job with is_public set.

Adds SELECT-only RLS policies that admit a public job, and its rows in the
job-scoped and junction tables, to any signed-in user. The column itself comes
from the previous migration, add_job_is_public.

Deliberately left out of public read access: ``job_data_files`` (user uploads
may be unpublished or restricted), ``job_chat_messages``, ``cost_records`` and
``job_shares``. Those stay owner/share-only.

Row-level security cannot restrict individual columns, and ``edit`` shares may
UPDATE ``jobs``, so a trigger stops anyone but the owner from changing
``is_public``. System operations that run without an ``app.current_user_id``
are not affected.

Revision ID: add_job_public_rls
Revises: add_job_is_public
Create Date: 2026-09-28 12:10:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "add_job_public_rls"
down_revision: str | None = "add_job_is_public"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNED_IN = "COALESCE(current_setting('app.current_user_id', TRUE), '') <> ''"

# Job-scoped tables whose rows become readable when their job is public.
PUBLIC_JOB_TABLES = (
    "hypotheses",
    "findings",
    "literature",
    "analysis_log",
    "iteration_summaries",
    "feedback_history",
    "plots",
)

# Junction tables: (table, [(parent table, junction column), ...]).
PUBLIC_JUNCTION_TABLES = (
    ("finding_hypotheses", (("findings", "finding_id"), ("hypotheses", "hypothesis_id"))),
    ("finding_literature", (("findings", "finding_id"), ("literature", "literature_id"))),
    ("hypothesis_spawns", (("hypotheses", "parent_id"), ("hypotheses", "child_id"))),
)


def upgrade() -> None:
    op.execute(
        f"""
        CREATE POLICY jobs_select_public ON jobs FOR SELECT
            USING ({_SIGNED_IN} AND is_public)
        """
    )
    for table in PUBLIC_JOB_TABLES:
        op.execute(
            f"""
            CREATE POLICY {table}_select_public ON {table} FOR SELECT
                USING (
                    {_SIGNED_IN}
                    AND EXISTS (
                        SELECT 1 FROM jobs
                        WHERE jobs.id = {table}.job_id AND jobs.is_public
                    )
                )
            """
        )
    for table, parents in PUBLIC_JUNCTION_TABLES:
        clauses = " OR ".join(
            f"""EXISTS (
                    SELECT 1 FROM {parent}
                    JOIN jobs ON jobs.id = {parent}.job_id
                    WHERE {parent}.id = {table}.{column} AND jobs.is_public
                )"""
            for parent, column in parents
        )
        op.execute(
            f"""
            CREATE POLICY {table}_select_public ON {table} FOR SELECT
                USING ({_SIGNED_IN} AND ({clauses}))
            """
        )

    op.execute(
        """
        CREATE FUNCTION jobs_guard_is_public() RETURNS trigger AS $$
        BEGIN
            IF NEW.is_public IS DISTINCT FROM OLD.is_public
               AND COALESCE(current_setting('app.current_user_id', TRUE), '') <> ''
               AND (
                   OLD.owner_id IS NULL
                   OR OLD.owner_id::text <> current_setting('app.current_user_id', TRUE)
               )
            THEN
                RAISE EXCEPTION 'only the job owner can change is_public'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER jobs_guard_is_public
            BEFORE UPDATE OF is_public ON jobs
            FOR EACH ROW EXECUTE FUNCTION jobs_guard_is_public()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS jobs_guard_is_public ON jobs")
    op.execute("DROP FUNCTION IF EXISTS jobs_guard_is_public()")
    for table, _parents in PUBLIC_JUNCTION_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_select_public ON {table}")
    for table in PUBLIC_JOB_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_select_public ON {table}")
    op.execute("DROP POLICY IF EXISTS jobs_select_public ON jobs")
