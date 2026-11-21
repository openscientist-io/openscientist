"""Add user_feedback, the free-text messages users send to the admins.

A user's session may insert a message under its own id and nothing else, so it
can neither read the messages nor mark one read. A trigger refuses a sender's
eleventh message within 24 hours. The admin login reads the messages and keeps
the read state. A deleted sender leaves the message with a NULL sender.

Revision ID: user_feedback
Revises: job_failed_phase
Create Date: 2026-11-18 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "user_feedback"
down_revision: str | None = "job_failed_phase"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE user_feedback (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            user_id uuid REFERENCES users (id) ON DELETE SET NULL,
            message text NOT NULL
                CONSTRAINT ck_user_feedback_message_length
                CHECK (char_length(message) BETWEEN 1 AND 5000),
            read_at timestamptz,
            read_by uuid REFERENCES users (id) ON DELETE SET NULL,
            CONSTRAINT ck_user_feedback_read_by_needs_read_at
                CHECK (read_by IS NULL OR read_at IS NOT NULL)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE user_feedback IS "
        "'Free-text feedback users send to the admins, with the admins'' read state'"
    )
    op.execute("COMMENT ON COLUMN user_feedback.user_id IS 'Sender, NULL once deleted'")
    op.execute("COMMENT ON COLUMN user_feedback.message IS 'The sender''s text'")
    op.execute("COMMENT ON COLUMN user_feedback.read_at IS 'When an admin marked it read'")
    op.execute("COMMENT ON COLUMN user_feedback.read_by IS 'Admin who marked it read'")
    op.execute("ALTER TABLE user_feedback ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_feedback FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY user_feedback_insert_own ON user_feedback FOR INSERT TO openscientist_app
            WITH CHECK (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND user_id::text = current_setting('app.current_user_id', TRUE)
            )
        """
    )
    # The default privileges hand every new table to the app role. A user's
    # session only inserts its message, so it cannot set the read state either.
    op.execute("REVOKE ALL ON user_feedback FROM openscientist_app")
    op.execute("GRANT INSERT (user_id, message) ON user_feedback TO openscientist_app")
    op.execute(
        "CREATE INDEX ix_user_feedback_user_id_created_at ON user_feedback (user_id, created_at)"
    )
    # SECURITY DEFINER, because the user's session may not read the rows it
    # counts. The advisory lock makes a sender's concurrent inserts wait for one
    # another, so each count sees the rows committed before it.
    op.execute(
        """
        CREATE FUNCTION user_feedback_daily_limit() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, pg_temp
        AS $$
        BEGIN
            IF NEW.user_id IS NULL THEN
                RETURN NEW;
            END IF;
            PERFORM pg_advisory_xact_lock(hashtext('user_feedback'), hashtext(NEW.user_id::text));
            IF (
                SELECT count(*) FROM public.user_feedback
                WHERE user_id = NEW.user_id AND created_at > now() - interval '24 hours'
            ) >= 10 THEN
                RAISE EXCEPTION 'feedback daily limit reached' USING ERRCODE = 'OSF01';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION user_feedback_daily_limit() FROM PUBLIC")
    op.execute(
        "CREATE TRIGGER user_feedback_daily_limit BEFORE INSERT ON user_feedback "
        "FOR EACH ROW EXECUTE FUNCTION user_feedback_daily_limit()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE user_feedback")
    op.execute("DROP FUNCTION user_feedback_daily_limit()")
