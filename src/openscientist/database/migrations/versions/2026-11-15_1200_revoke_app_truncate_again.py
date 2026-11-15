"""Take TRUNCATE from the app role again.

Every bootstrap run used to grant the app role all table privileges in the
owner's default privileges, so a run after the narrowing migration brought
TRUNCATE back for every table created afterwards. Row-level security does not
apply to TRUNCATE.

Revision ID: revoke_app_truncate_again
Revises: analysis_log_outputs
Create Date: 2026-11-15 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "revoke_app_truncate_again"
down_revision: str | None = "analysis_log_outputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("REVOKE TRUNCATE ON ALL TABLES IN SCHEMA public FROM openscientist_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE openscientist_owner IN SCHEMA public "
        "REVOKE TRUNCATE ON TABLES FROM openscientist_app"
    )


def downgrade() -> None:
    # The grants before the upgrade depended on when the bootstrap last ran, so
    # the app role stays without TRUNCATE.
    pass
