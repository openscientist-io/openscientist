"""Narrow the grants and policies past what the code uses.

An ``edit`` share may UPDATE the shared job, and a policy cannot fence
columns, so the app role's UPDATE on ``jobs`` narrows to the run columns the
web updates for a share plus ``is_public``, which its trigger keeps
owner-only. The junction tables split into a SELECT policy for any share and
write policies for owners and ``edit`` shares. The SECURITY DEFINER helpers
lose their PUBLIC execute, and TRUNCATE leaves the web logins.

Revision ID: narrow_database_grants
Revises: job_is_public
Create Date: 2026-11-03 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "narrow_database_grants"
down_revision: str | None = "job_is_public"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The run columns the web writes on a job it operates. set_job_status moves
# status, started_at, ended_at, error_message, stop_note and
# cancellation_reason, the queue path writes queue_reason, the knowledge state
# writes the iteration columns, the provenance stamp writes version_info, and
# the owner's is_public toggle is kept owner-only by its trigger. updated_at is
# stamped by the ORM on every update. The definition columns, owner_id and
# created_at stay out of reach of the web role.
_KEPT_JOBS_COLUMNS = (
    "status",
    "current_iteration",
    "data_summary",
    "agent_status",
    "agent_status_updated_at",
    "consensus_answer",
    "started_at",
    "ended_at",
    "error_message",
    "cancellation_reason",
    "stop_note",
    "queue_reason",
    "version_info",
    "is_public",
    "updated_at",
)

# Junction table, then the parent table and column tying each side to a job.
_JUNCTION_TABLES = (
    ("finding_hypotheses", (("findings", "finding_id"), ("hypotheses", "hypothesis_id"))),
    ("finding_literature", (("findings", "finding_id"), ("literature", "literature_id"))),
)

_SIGNED_IN = "current_setting('app.current_user_id', TRUE) IS NOT NULL"


def _side(table: str, parent: str, column: str, edit_only: bool) -> str:
    level = " AND job_shares.permission_level = 'edit'" if edit_only else ""
    return f"""
        EXISTS (
            SELECT 1 FROM {parent}
            JOIN jobs ON jobs.id = {parent}.job_id
            WHERE {parent}.id = {table}.{column}
            AND (
                jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
                OR EXISTS (
                    SELECT 1 FROM job_shares
                    WHERE job_shares.job_id = jobs.id
                    AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                    {level}
                )
            )
        )"""


def _visible(table: str, parents: tuple[tuple[str, str], ...], edit_only: bool) -> str:
    return " OR ".join(_side(table, parent, column, edit_only) for parent, column in parents)


def upgrade() -> None:
    # A column-level revoke cannot narrow a table-level grant, so the UPDATE
    # leaves the app role first and the kept columns are granted back.
    op.execute("REVOKE UPDATE ON jobs FROM openscientist_app")
    op.execute(
        "GRANT UPDATE ({}) ON jobs TO openscientist_app".format(", ".join(_KEPT_JOBS_COLUMNS))
    )

    for table, parents in _JUNCTION_TABLES:
        op.execute(f"DROP POLICY {table}_all ON {table}")
        op.execute(
            f"CREATE POLICY {table}_select_shared ON {table} FOR SELECT TO openscientist_app "
            f"USING ({_SIGNED_IN} AND ({_visible(table, parents, edit_only=False)}))"
        )
        op.execute(
            f"CREATE POLICY {table}_insert_shared_edit ON {table} FOR INSERT TO openscientist_app "
            f"WITH CHECK ({_SIGNED_IN} AND ({_visible(table, parents, edit_only=True)}))"
        )
        for command in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE POLICY {table}_{command.lower()}_shared_edit ON {table} FOR {command} "
                f"TO openscientist_app USING ({_SIGNED_IN} AND ({_visible(table, parents, edit_only=True)}))"
            )

    # The openscientist_app policies are the only callers; the definer context
    # of current_user_is_admin runs as the function owner, which keeps its own
    # execute on is_admin.
    for function in ("check_job_owner(uuid, text)", "is_admin(uuid)", "current_user_is_admin()"):
        op.execute(f"REVOKE EXECUTE ON FUNCTION {function} FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION check_job_owner(uuid, text) TO openscientist_app")
    op.execute("GRANT EXECUTE ON FUNCTION current_user_is_admin() TO openscientist_app")

    # Row-level security does not apply to TRUNCATE.
    op.execute("REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM openscientist_app")
    op.execute("REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM openscientist_agent")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE openscientist_owner IN SCHEMA public "
        "REVOKE TRUNCATE ON TABLES FROM openscientist_app"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE openscientist_owner IN SCHEMA public "
        "GRANT TRUNCATE ON TABLES TO openscientist_app"
    )
    op.execute("GRANT TRUNCATE ON ALL TABLES IN SCHEMA public TO openscientist_app")
    for function in ("check_job_owner(uuid, text)", "is_admin(uuid)", "current_user_is_admin()"):
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO PUBLIC")
    for table, parents in _JUNCTION_TABLES:
        op.execute(f"DROP POLICY {table}_delete_shared_edit ON {table}")
        op.execute(f"DROP POLICY {table}_update_shared_edit ON {table}")
        op.execute(f"DROP POLICY {table}_insert_shared_edit ON {table}")
        op.execute(f"DROP POLICY {table}_select_shared ON {table}")
        op.execute(
            f"CREATE POLICY {table}_all ON {table} FOR ALL TO openscientist_app "
            f"USING ({_visible(table, parents, edit_only=False)})"
        )
    op.execute(
        "REVOKE UPDATE ({}) ON jobs FROM openscientist_app".format(", ".join(_KEPT_JOBS_COLUMNS))
    )
    op.execute("GRANT UPDATE ON jobs TO openscientist_app")
