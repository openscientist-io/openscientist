"""Add row-level security policies

Revision ID: a3f9c2d1e4b7
Revises: ebc9b8f1cdd1
Create Date: 2026-02-08 15:20:00.000000+00:00

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f9c2d1e4b7"
down_revision: Union[str, None] = "ebc9b8f1cdd1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Enable Row-Level Security (RLS) on all tables and create policies.

    RLS Strategy:
    - User-owned tables: Users can only access their own records
    - Job-related tables: Access controlled by job ownership and sharing
    - Junction tables: Inherit access from parent tables
    """

    # =========================================================================
    # HELPER FUNCTION - Breaks circular RLS dependency between jobs/job_shares
    # =========================================================================
    # The jobs table has policies that query job_shares, and job_shares has
    # policies that query jobs. This creates infinite recursion. A SECURITY
    # DEFINER function runs as the function owner (superuser), bypassing RLS
    # on the referenced table and breaking the cycle.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION check_job_owner(p_job_id UUID, p_user_id TEXT)
        RETURNS BOOLEAN AS $$
            SELECT EXISTS (
                SELECT 1 FROM jobs
                WHERE id = p_job_id AND owner_id::text = p_user_id
            )
        $$ LANGUAGE SQL SECURITY DEFINER STABLE
    """
    )

    # =========================================================================
    # USER-OWNED TABLES
    # =========================================================================

    # Users table - users can read their own record, admins can read all
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY users_select_own
            ON users
            FOR SELECT
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND id::text = current_setting('app.current_user_id', TRUE)
            )
    """
    )
    op.execute(
        """
        CREATE POLICY users_bypass ON users FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )

    # API keys - users can manage their own keys
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_keys FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY api_keys_all_own
            ON api_keys
            FOR ALL
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND user_id::text = current_setting('app.current_user_id', TRUE)
            )
    """
    )
    op.execute(
        """
        CREATE POLICY api_keys_bypass ON api_keys FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )

    # Sessions - users can manage their own sessions
    op.execute("ALTER TABLE sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY sessions_all_own
            ON sessions
            FOR ALL
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND user_id::text = current_setting('app.current_user_id', TRUE)
            )
    """
    )
    op.execute(
        """
        CREATE POLICY sessions_bypass ON sessions FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )

    # OAuth accounts - users can access their own OAuth accounts
    op.execute("ALTER TABLE oauth_accounts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE oauth_accounts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY oauth_accounts_all_own
            ON oauth_accounts
            FOR ALL
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND user_id::text = current_setting('app.current_user_id', TRUE)
            )
    """
    )
    op.execute(
        """
        CREATE POLICY oauth_accounts_bypass ON oauth_accounts FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )

    # =========================================================================
    # JOB TABLE - Foundation for job-related access control
    # =========================================================================

    op.execute("ALTER TABLE jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")

    # Policy 1: Job owner has full access
    op.execute(
        """
        CREATE POLICY jobs_all_owner
            ON jobs
            FOR ALL
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND owner_id::text = current_setting('app.current_user_id', TRUE)
            )
    """
    )

    # Policy 2: Users with 'view' or 'edit' share can read
    op.execute(
        """
        CREATE POLICY jobs_select_shared
            ON jobs
            FOR SELECT
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM job_shares
                    WHERE job_shares.job_id = jobs.id
                    AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                )
            )
    """
    )

    # Policy 3: Users with 'edit' share can update
    op.execute(
        """
        CREATE POLICY jobs_update_shared_edit
            ON jobs
            FOR UPDATE
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM job_shares
                    WHERE job_shares.job_id = jobs.id
                    AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                    AND job_shares.permission_level = 'edit'
                )
            )
    """
    )

    # Policy 4: Allow access to orphaned jobs (owner_id IS NULL) for all users
    # This is temporary until orphaned jobs are assigned to users
    op.execute(
        """
        CREATE POLICY jobs_select_orphaned
            ON jobs
            FOR SELECT
            USING (owner_id IS NULL)
    """
    )
    op.execute(
        """
        CREATE POLICY jobs_bypass ON jobs FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )

    # =========================================================================
    # JOB SHARES - Special handling for share management
    # =========================================================================

    op.execute("ALTER TABLE job_shares ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE job_shares FORCE ROW LEVEL SECURITY")

    # Job owner can manage all shares for their jobs.
    # Uses check_job_owner() SECURITY DEFINER function to avoid infinite
    # recursion between jobs and job_shares RLS policies.
    op.execute(
        """
        CREATE POLICY job_shares_all_owner
            ON job_shares
            FOR ALL
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND check_job_owner(job_id, current_setting('app.current_user_id', TRUE))
            )
    """
    )

    # Users can read shares where they are the recipient
    op.execute(
        """
        CREATE POLICY job_shares_select_recipient
            ON job_shares
            FOR SELECT
            USING (
                current_setting('app.current_user_id', TRUE) IS NOT NULL
                AND shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
            )
    """
    )
    op.execute(
        """
        CREATE POLICY job_shares_bypass ON job_shares FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )

    # =========================================================================
    # JOB-RELATED TABLES - Inherit access from jobs table
    # =========================================================================

    # Helper function to create policies for tables with job_id foreign key
    def create_job_related_policies(table_name: str, allow_insert: bool = True):
        """
        Create RLS policies for tables that reference jobs.

        Args:
            table_name: Name of the table
            allow_insert: Whether to allow INSERT for shared edit users
        """
        # Enable RLS
        op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")

        # Bypass policy for admin operations
        op.execute(
            f"""
            CREATE POLICY {table_name}_bypass ON {table_name} FOR ALL
                USING (current_setting('app.bypass_rls', TRUE) = 'true')
        """
        )

        # Job owner has full access
        op.execute(
            f"""
            CREATE POLICY {table_name}_all_owner
                ON {table_name}
                FOR ALL
                USING (
                    current_setting('app.current_user_id', TRUE) IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM jobs
                        WHERE jobs.id = {table_name}.job_id
                        AND jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
                    )
                )
        """
        )

        # Users with any job share can read
        op.execute(
            f"""
            CREATE POLICY {table_name}_select_shared
                ON {table_name}
                FOR SELECT
                USING (
                    current_setting('app.current_user_id', TRUE) IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM jobs
                        JOIN job_shares ON job_shares.job_id = jobs.id
                        WHERE jobs.id = {table_name}.job_id
                        AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                    )
                )
        """
        )

        if allow_insert:
            # Users with 'edit' share can insert/update/delete
            op.execute(
                f"""
                CREATE POLICY {table_name}_modify_shared_edit
                    ON {table_name}
                    FOR INSERT
                    WITH CHECK (
                        current_setting('app.current_user_id', TRUE) IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM jobs
                            JOIN job_shares ON job_shares.job_id = jobs.id
                            WHERE jobs.id = {table_name}.job_id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                            AND job_shares.permission_level = 'edit'
                        )
                    )
            """
            )

            op.execute(
                f"""
                CREATE POLICY {table_name}_update_shared_edit
                    ON {table_name}
                    FOR UPDATE
                    USING (
                        current_setting('app.current_user_id', TRUE) IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM jobs
                            JOIN job_shares ON job_shares.job_id = jobs.id
                            WHERE jobs.id = {table_name}.job_id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                            AND job_shares.permission_level = 'edit'
                        )
                    )
            """
            )

            op.execute(
                f"""
                CREATE POLICY {table_name}_delete_shared_edit
                    ON {table_name}
                    FOR DELETE
                    USING (
                        current_setting('app.current_user_id', TRUE) IS NOT NULL
                        AND EXISTS (
                            SELECT 1 FROM jobs
                            JOIN job_shares ON job_shares.job_id = jobs.id
                            WHERE jobs.id = {table_name}.job_id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                            AND job_shares.permission_level = 'edit'
                        )
                    )
            """
            )

    # Apply policies to all job-related tables
    job_tables = [
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
    ]

    for table in job_tables:
        create_job_related_policies(table)

    # =========================================================================
    # JUNCTION TABLES - Inherit from parent entities
    # =========================================================================

    # finding_hypotheses - Access through findings or hypotheses
    op.execute("ALTER TABLE finding_hypotheses ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finding_hypotheses FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY finding_hypotheses_bypass ON finding_hypotheses FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )
    op.execute(
        """
        CREATE POLICY finding_hypotheses_all
            ON finding_hypotheses
            FOR ALL
            USING (
                -- Has access through finding
                EXISTS (
                    SELECT 1 FROM findings
                    JOIN jobs ON jobs.id = findings.job_id
                    WHERE findings.id = finding_hypotheses.finding_id
                    AND (
                        jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
                        OR EXISTS (
                            SELECT 1 FROM job_shares
                            WHERE job_shares.job_id = jobs.id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                        )
                    )
                )
                OR
                -- Has access through hypothesis
                EXISTS (
                    SELECT 1 FROM hypotheses
                    JOIN jobs ON jobs.id = hypotheses.job_id
                    WHERE hypotheses.id = finding_hypotheses.hypothesis_id
                    AND (
                        jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
                        OR EXISTS (
                            SELECT 1 FROM job_shares
                            WHERE job_shares.job_id = jobs.id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                        )
                    )
                )
            )
    """
    )

    # finding_literature - Access through findings or literature
    op.execute("ALTER TABLE finding_literature ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE finding_literature FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY finding_literature_bypass ON finding_literature FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )
    op.execute(
        """
        CREATE POLICY finding_literature_all
            ON finding_literature
            FOR ALL
            USING (
                -- Has access through finding
                EXISTS (
                    SELECT 1 FROM findings
                    JOIN jobs ON jobs.id = findings.job_id
                    WHERE findings.id = finding_literature.finding_id
                    AND (
                        jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
                        OR EXISTS (
                            SELECT 1 FROM job_shares
                            WHERE job_shares.job_id = jobs.id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                        )
                    )
                )
                OR
                -- Has access through literature
                EXISTS (
                    SELECT 1 FROM literature
                    JOIN jobs ON jobs.id = literature.job_id
                    WHERE literature.id = finding_literature.literature_id
                    AND (
                        jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
                        OR EXISTS (
                            SELECT 1 FROM job_shares
                            WHERE job_shares.job_id = jobs.id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                        )
                    )
                )
            )
    """
    )

    # hypothesis_spawns - Access through parent or child hypothesis
    op.execute("ALTER TABLE hypothesis_spawns ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hypothesis_spawns FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY hypothesis_spawns_bypass ON hypothesis_spawns FOR ALL
            USING (current_setting('app.bypass_rls', TRUE) = 'true')
    """
    )
    op.execute(
        """
        CREATE POLICY hypothesis_spawns_all
            ON hypothesis_spawns
            FOR ALL
            USING (
                -- Has access through parent hypothesis
                EXISTS (
                    SELECT 1 FROM hypotheses
                    JOIN jobs ON jobs.id = hypotheses.job_id
                    WHERE hypotheses.id = hypothesis_spawns.parent_id
                    AND (
                        jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
                        OR EXISTS (
                            SELECT 1 FROM job_shares
                            WHERE job_shares.job_id = jobs.id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                        )
                    )
                )
                OR
                -- Has access through child hypothesis
                EXISTS (
                    SELECT 1 FROM hypotheses
                    JOIN jobs ON jobs.id = hypotheses.job_id
                    WHERE hypotheses.id = hypothesis_spawns.child_id
                    AND (
                        jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
                        OR EXISTS (
                            SELECT 1 FROM job_shares
                            WHERE job_shares.job_id = jobs.id
                            AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
                        )
                    )
                )
            )
    """
    )


def downgrade() -> None:
    """Disable RLS and drop all policies."""

    # Drop policies and disable RLS for user-owned tables
    for table in ["users", "api_keys", "sessions", "oauth_accounts"]:
        op.execute(f"DROP POLICY IF EXISTS {table}_select_own ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_all_own ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_bypass ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop policies and disable RLS for jobs table
    op.execute("DROP POLICY IF EXISTS jobs_all_owner ON jobs")
    op.execute("DROP POLICY IF EXISTS jobs_select_shared ON jobs")
    op.execute("DROP POLICY IF EXISTS jobs_update_shared_edit ON jobs")
    op.execute("DROP POLICY IF EXISTS jobs_select_orphaned ON jobs")
    op.execute("DROP POLICY IF EXISTS jobs_bypass ON jobs")
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs DISABLE ROW LEVEL SECURITY")

    # Drop policies and disable RLS for job_shares
    op.execute("DROP POLICY IF EXISTS job_shares_all_owner ON job_shares")
    op.execute("DROP POLICY IF EXISTS job_shares_select_recipient ON job_shares")
    op.execute("DROP POLICY IF EXISTS job_shares_bypass ON job_shares")
    op.execute("ALTER TABLE job_shares NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE job_shares DISABLE ROW LEVEL SECURITY")

    # Drop policies and disable RLS for job-related tables
    job_tables = [
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
    ]

    for table in job_tables:
        op.execute(f"DROP POLICY IF EXISTS {table}_all_owner ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_select_shared ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_modify_shared_edit ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_update_shared_edit ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_delete_shared_edit ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_bypass ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop policies and disable RLS for junction tables
    for table in ["finding_hypotheses", "finding_literature", "hypothesis_spawns"]:
        op.execute(f"DROP POLICY IF EXISTS {table}_all ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_bypass ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop helper function
    op.execute("DROP FUNCTION IF EXISTS check_job_owner(UUID, TEXT)")
