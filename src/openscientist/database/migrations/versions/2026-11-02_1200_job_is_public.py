"""Add jobs.is_public, the flag the owner sets to make a job readable by any
signed-in user.

Default false, so every existing job stays private. SELECT-only policies,
scoped to the web's app role, admit a public job and its rows in the analysis
tables to any session with a signed-in user. Uploaded inputs (job_data_files),
chat messages, cost records and shares stay owner/share-only.

Row-level security cannot restrict a single column, and an ``edit`` share may
UPDATE ``jobs``, so a trigger refuses a change to ``is_public`` from anyone but
the owner. A session with no signed-in user (the admin and agent logins) is
unaffected, and the agent never touches the column.

Revision ID: job_is_public
Revises: job_stop_note
Create Date: 2026-11-02 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "job_is_public"
down_revision: str | None = "job_stop_note"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A session with a signed-in user: the RLS context is set and non-empty.
_SIGNED_IN = "COALESCE(current_setting('app.current_user_id', TRUE), '') <> ''"

# Job-scoped tables whose rows become readable when their job is public.
_PUBLIC_JOB_TABLES = (
    "hypotheses",
    "findings",
    "literature",
    "analysis_log",
    "iteration_summaries",
    "feedback_history",
    "plots",
)

# Junction tables, with the parent table and column tying each side to a job.
_PUBLIC_JUNCTION_TABLES = (
    ("finding_hypotheses", (("findings", "finding_id"), ("hypotheses", "hypothesis_id"))),
    ("finding_literature", (("findings", "finding_id"), ("literature", "literature_id"))),
)


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="The owner has made this job readable by any signed-in user",
        ),
    )

    op.execute(
        f"""
        CREATE POLICY jobs_select_public ON jobs FOR SELECT TO openscientist_app
            USING ({_SIGNED_IN} AND is_public)
        """
    )

    for table in _PUBLIC_JOB_TABLES:
        op.execute(
            f"""
            CREATE POLICY {table}_select_public ON {table} FOR SELECT TO openscientist_app
                USING (
                    {_SIGNED_IN}
                    AND EXISTS (
                        SELECT 1 FROM jobs
                        WHERE jobs.id = {table}.job_id AND jobs.is_public
                    )
                )
            """
        )

    for table, parents in _PUBLIC_JUNCTION_TABLES:
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
            CREATE POLICY {table}_select_public ON {table} FOR SELECT TO openscientist_app
                USING ({_SIGNED_IN} AND ({clauses}))
            """
        )

    # The flag is owner-only even though an edit share may UPDATE jobs. A
    # no-user session (admin, agent) is system work and passes through.
    op.execute(
        """
        CREATE FUNCTION jobs_guard_is_public() RETURNS trigger AS $$
        BEGIN
            IF NEW.is_public IS DISTINCT FROM OLD.is_public
               AND COALESCE(current_setting('app.current_user_id', TRUE), '') <> ''
               AND COALESCE(OLD.owner_id::text, '') <> current_setting('app.current_user_id', TRUE)
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
    for table, _parents in _PUBLIC_JUNCTION_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_select_public ON {table}")
    for table in _PUBLIC_JOB_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {table}_select_public ON {table}")
    op.execute("DROP POLICY IF EXISTS jobs_select_public ON jobs")
    op.drop_column("jobs", "is_public")
