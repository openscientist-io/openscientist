"""Let an admin's own session grant and remove admin rights.

A user session inserts an ``administrators`` row only while its user is an
admin and records itself as ``granted_by``, and deletes a row only while its
user is an admin. A trigger serialises these writes on an advisory lock,
refuses a session whose user lost admin rights to a write committed meanwhile,
and refuses an admin removing their own row, so two admins removing each other
at once leave one admin. Sessions that act for no user, the BYPASSRLS admin
login and a deleted user's cascade, pass the trigger unchecked.

Revision ID: admins_grant_admins
Revises: max_iterations_cap
Create Date: 2026-11-21 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "admins_grant_admins"
down_revision: str | None = "max_iterations_cap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTOR = "current_setting('app.current_user_id', TRUE)"


def upgrade() -> None:
    op.execute("REVOKE ALL ON administrators FROM openscientist_app")
    op.execute("GRANT SELECT, DELETE ON administrators TO openscientist_app")
    op.execute("GRANT INSERT (user_id, granted_by) ON administrators TO openscientist_app")
    op.execute(
        f"""
        CREATE POLICY administrators_insert_admin ON administrators FOR INSERT
            TO openscientist_app
            WITH CHECK (current_user_is_admin() AND granted_by::text = {_ACTOR})
        """
    )
    op.execute(
        """
        CREATE POLICY administrators_delete_admin ON administrators FOR DELETE
            TO openscientist_app
            USING (current_user_is_admin())
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION administrators_actor_check() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        DECLARE
            actor text := NULLIF({_ACTOR}, '');
        BEGIN
            IF actor IS NULL THEN
                RETURN COALESCE(NEW, OLD);
            END IF;
            PERFORM pg_advisory_xact_lock(hashtext('administrators'));
            IF NOT EXISTS (SELECT 1 FROM public.administrators WHERE user_id::text = actor) THEN
                RAISE EXCEPTION 'only admins can change admin rights' USING ERRCODE = 'OSA01';
            END IF;
            IF TG_OP = 'DELETE' AND OLD.user_id::text = actor THEN
                RAISE EXCEPTION 'admins cannot remove their own admin rights'
                    USING ERRCODE = 'OSA02';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION administrators_actor_check() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER administrators_actor_check BEFORE INSERT OR DELETE ON administrators "
        "FOR EACH ROW EXECUTE FUNCTION administrators_actor_check()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER administrators_actor_check ON administrators")
    op.execute("DROP FUNCTION administrators_actor_check()")
    op.execute("DROP POLICY administrators_delete_admin ON administrators")
    op.execute("DROP POLICY administrators_insert_admin ON administrators")
    op.execute("REVOKE ALL ON administrators FROM openscientist_app")
    op.execute("GRANT ALL ON administrators TO openscientist_app")
