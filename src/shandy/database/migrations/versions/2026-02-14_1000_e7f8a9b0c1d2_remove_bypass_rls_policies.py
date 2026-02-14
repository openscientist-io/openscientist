"""Remove bypass_rls session variable policies

Revision ID: e7f8a9b0c1d2
Revises: d6a7b8c9e0f1
Create Date: 2026-02-14 10:00:00.000000+00:00

The bypass_rls session variable approach has been replaced with the dual-engine
pattern using a PostgreSQL role with BYPASSRLS privilege (shandy_admin).

This migration removes all the *_bypass policies that checked the
app.bypass_rls session variable. RLS bypass is now handled at the database
role level, which is more secure.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6a7b8c9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# All tables that have bypass policies
TABLES_WITH_BYPASS_POLICIES = [
    # User-owned tables
    "users",
    "api_keys",
    "sessions",
    "oauth_accounts",
    # Job tables
    "jobs",
    "job_shares",
    "job_data_files",
    "job_chat_messages",
    "hypotheses",
    "findings",
    "literature",
    "analysis_log",
    "iteration_summaries",
    "feedback_history",
    "plots",
    "cost_records",
    # Junction tables
    "finding_hypotheses",
    "finding_literature",
    "hypothesis_spawns",
    # Skills tables
    "skill_sources",
    "skills",
    "job_skills",
    # Admin tables
    "administrators",
]


def upgrade() -> None:
    """Remove all bypass_rls session variable policies and create admin role.

    RLS bypass is now handled by the shandy_admin PostgreSQL role with
    BYPASSRLS privilege. This migration creates the role for existing databases
    (fresh installs create it via docker/postgres/init.sql).
    """
    # Create shandy_admin role with BYPASSRLS privilege
    # This role bypasses RLS policies at the database level
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'shandy_admin') THEN
                CREATE ROLE shandy_admin WITH LOGIN PASSWORD 'shandy_dev_password' BYPASSRLS;
            END IF;
        END
        $$
        """
    )

    # Grant privileges to shandy_admin
    op.execute("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO shandy_admin")
    op.execute("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO shandy_admin")
    op.execute("GRANT USAGE ON SCHEMA public TO shandy_admin")

    # Grant default privileges for future tables
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT ALL PRIVILEGES ON TABLES TO shandy_admin
        """
    )
    op.execute(
        """
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT ALL PRIVILEGES ON SEQUENCES TO shandy_admin
        """
    )

    # Remove bypass policies
    for table in TABLES_WITH_BYPASS_POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {table}_bypass ON {table}")


def downgrade() -> None:
    """Recreate bypass policies for backwards compatibility.

    Note: This is only needed if reverting to the session variable approach.
    """
    for table in TABLES_WITH_BYPASS_POLICIES:
        op.execute(
            f"""
            CREATE POLICY {table}_bypass ON {table} FOR ALL
                USING (current_setting('app.bypass_rls', TRUE) = 'true')
        """
        )
