"""Fix the search_path of the SECURITY DEFINER helpers the RLS policies call.

is_admin, current_user_is_admin and check_job_owner named their tables without a
schema and had no search_path of their own, so they resolved names through the
caller's path, where temporary tables come first. A session could shadow
administrators or jobs and pass the admin and owner checks.

Revision ID: definer_search_path
Revises: users_update_own_ntfy
Create Date: 2026-10-25 15:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "definer_search_path"
down_revision: str | None = "users_update_own_ntfy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_JOB_OWNER = """
    CREATE OR REPLACE FUNCTION check_job_owner(p_job_id UUID, p_user_id TEXT)
    RETURNS BOOLEAN AS $$
        SELECT EXISTS (
            SELECT 1 FROM {schema}jobs
            WHERE id = p_job_id AND owner_id::text = p_user_id
        )
    $$ LANGUAGE SQL SECURITY DEFINER STABLE {search_path}
"""

_IS_ADMIN = """
    CREATE OR REPLACE FUNCTION is_admin(check_user_id UUID)
    RETURNS BOOLEAN AS $$
    BEGIN
        RETURN EXISTS (
            SELECT 1 FROM {schema}administrators WHERE user_id = check_user_id
        );
    END;
    $$ LANGUAGE plpgsql SECURITY DEFINER {search_path}
"""

_CURRENT_USER_IS_ADMIN = """
    CREATE OR REPLACE FUNCTION current_user_is_admin()
    RETURNS BOOLEAN AS $$
    DECLARE
        uid UUID;
    BEGIN
        BEGIN
            uid := current_setting('app.current_user_id', TRUE)::UUID;
        EXCEPTION WHEN OTHERS THEN
            RETURN FALSE;
        END;
        IF uid IS NULL THEN
            RETURN FALSE;
        END IF;
        RETURN {schema}is_admin(uid);
    END;
    $$ LANGUAGE plpgsql SECURITY DEFINER {search_path}
"""

_FUNCTIONS = (_CHECK_JOB_OWNER, _IS_ADMIN, _CURRENT_USER_IS_ADMIN)


def upgrade() -> None:
    for definition in _FUNCTIONS:
        op.execute(
            definition.format(schema="public.", search_path="SET search_path = pg_catalog, pg_temp")
        )


def downgrade() -> None:
    # CREATE OR REPLACE drops the SET clause, which restores the old definitions.
    for definition in _FUNCTIONS:
        op.execute(definition.format(schema="", search_path=""))
