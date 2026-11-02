"""Add the admin settings row and its change history.

The one admin_settings row holds the runtime policy an admin edits at run time,
with a NULL budget meaning no limit. admin_setting_changes records every edit,
who made it and the old and new value, for the page's history.

Revision ID: admin_settings
Revises: drop_draft_schema
Create Date: 2026-10-31 13:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "admin_settings"
down_revision: str | None = "drop_draft_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE admin_settings (
            id boolean PRIMARY KEY DEFAULT true CHECK (id),
            max_job_cost_usd double precision
                CONSTRAINT ck_admin_settings_max_job_cost_usd CHECK (max_job_cost_usd > 0),
            max_project_spend_total_usd double precision
                CONSTRAINT ck_admin_settings_max_project_spend_total_usd
                CHECK (max_project_spend_total_usd > 0),
            max_project_spend_24h_usd double precision
                CONSTRAINT ck_admin_settings_max_project_spend_24h_usd
                CHECK (max_project_spend_24h_usd > 0),
            warn_project_spend_24h_usd double precision
                CONSTRAINT ck_admin_settings_warn_project_spend_24h_usd
                CHECK (warn_project_spend_24h_usd > 0),
            agent_cpu double precision
                CONSTRAINT ck_admin_settings_agent_cpu CHECK (agent_cpu > 0),
            executor_cpu double precision
                CONSTRAINT ck_admin_settings_executor_cpu CHECK (executor_cpu > 0),
            agent_memory_mib integer
                CONSTRAINT ck_admin_settings_agent_memory_mib CHECK (agent_memory_mib > 0),
            executor_memory_mib integer
                CONSTRAINT ck_admin_settings_executor_memory_mib
                CHECK (executor_memory_mib > 0),
            agent_timeout_seconds integer
                CONSTRAINT ck_admin_settings_agent_timeout_seconds
                CHECK (agent_timeout_seconds > 0),
            executor_timeout_seconds integer
                CONSTRAINT ck_admin_settings_executor_timeout_seconds
                CHECK (executor_timeout_seconds > 0),
            max_file_size_mb integer
                CONSTRAINT ck_admin_settings_max_file_size_mb CHECK (max_file_size_mb > 0),
            max_concurrent_jobs integer
                CONSTRAINT ck_admin_settings_max_concurrent_jobs
                CHECK (max_concurrent_jobs >= 0),
            scheduler_window_hours double precision
                CONSTRAINT ck_admin_settings_scheduler_window_hours
                CHECK (scheduler_window_hours > 0),
            airgapped boolean NOT NULL,
            session_duration_days integer
                CONSTRAINT ck_admin_settings_session_duration_days
                CHECK (session_duration_days >= 1),
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by uuid REFERENCES users (id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE admin_settings IS "
        "'The runtime policy an admin edits at run time, one row per deployment'"
    )
    op.execute(
        """
        CREATE TABLE admin_setting_changes (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            changed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            changed_by uuid REFERENCES users (id) ON DELETE SET NULL,
            setting text NOT NULL,
            old_value text,
            new_value text
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE admin_setting_changes IS "
        "'Who edited which admin setting, when, and from what to what'"
    )
    op.execute(
        "CREATE INDEX ix_admin_setting_changes_changed_at ON admin_setting_changes (changed_at)"
    )
    # The default privileges hand every new table to the app role, which may only read the row.
    op.execute("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON admin_settings FROM openscientist_app")
    # The history is the admin's alone, the app role cannot even read it.
    op.execute("REVOKE ALL ON admin_setting_changes FROM openscientist_app")


def downgrade() -> None:
    op.execute("DROP TABLE admin_setting_changes")
    op.execute("DROP TABLE admin_settings")
