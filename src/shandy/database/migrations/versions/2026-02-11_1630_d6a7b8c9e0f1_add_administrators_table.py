"""Add administrators table

Revision ID: d6a7b8c9e0f1
Revises: c5f2d4e3a1b8
Create Date: 2026-02-11 16:30:00.000000+00:00

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6a7b8c9e0f1"
down_revision: Union[str, None] = "c5f2d4e3a1b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create administrators table and update RLS policies."""

    # =========================================================================
    # ADMINISTRATORS - Users with admin privileges
    # =========================================================================
    op.create_table(
        "administrators",
        sa.Column(
            "user_id",
            sa.UUID(),
            nullable=False,
            comment="User who has admin privileges",
        ),
        sa.Column(
            "granted_by",
            sa.UUID(),
            nullable=True,
            comment="User who granted admin privileges (NULL for initial setup)",
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="Timestamp when admin was granted (UTC)",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="Optional notes about why admin was granted",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )

    # =========================================================================
    # HELPER FUNCTION TO CHECK ADMIN STATUS (avoids RLS recursion)
    # =========================================================================
    op.execute(
        """
        CREATE OR REPLACE FUNCTION is_admin(check_user_id UUID)
        RETURNS BOOLEAN AS $$
        BEGIN
            RETURN EXISTS (
                SELECT 1 FROM administrators WHERE user_id = check_user_id
            );
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER
    """
    )

    # Wrapper to check current session user
    op.execute(
        """
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
            RETURN is_admin(uid);
        END;
        $$ LANGUAGE plpgsql SECURITY DEFINER
    """
    )

    # =========================================================================
    # ROW-LEVEL SECURITY FOR ADMINISTRATORS
    # =========================================================================
    op.execute("ALTER TABLE administrators ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE administrators FORCE ROW LEVEL SECURITY")

    # Only admins can view the administrators table (uses SECURITY DEFINER function)
    op.execute(
        """
        CREATE POLICY administrators_select_admin ON administrators FOR SELECT
            USING (current_user_is_admin())
    """
    )

    # Bypass policy for system operations
    op.execute(
        """
        CREATE POLICY administrators_bypass ON administrators FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )

    # =========================================================================
    # UPDATE SKILL_SOURCES RLS POLICY
    # =========================================================================
    # Drop the old bypass-only policy
    op.execute("DROP POLICY IF EXISTS skill_sources_bypass ON skill_sources")

    # Create new policy that allows admin users
    op.execute(
        """
        CREATE POLICY skill_sources_admin ON skill_sources FOR ALL
            USING (current_user_is_admin())
    """
    )

    # Keep bypass policy for system operations (background scheduler, etc.)
    op.execute(
        """
        CREATE POLICY skill_sources_bypass ON skill_sources FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )


def downgrade() -> None:
    """Drop administrators table and revert RLS policies."""

    # Revert skill_sources RLS policies
    op.execute("DROP POLICY IF EXISTS skill_sources_admin ON skill_sources")
    op.execute("DROP POLICY IF EXISTS skill_sources_bypass ON skill_sources")
    op.execute(
        """
        CREATE POLICY skill_sources_bypass ON skill_sources FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )

    # Drop administrators RLS policies
    op.execute("DROP POLICY IF EXISTS administrators_select_admin ON administrators")
    op.execute("DROP POLICY IF EXISTS administrators_bypass ON administrators")
    op.execute("ALTER TABLE administrators NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE administrators DISABLE ROW LEVEL SECURITY")

    # Drop administrators table
    op.drop_table("administrators")

    # Drop helper functions
    op.execute("DROP FUNCTION IF EXISTS current_user_is_admin()")
    op.execute("DROP FUNCTION IF EXISTS is_admin(UUID)")
