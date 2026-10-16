"""Allow all DB roles to SELECT from experts.

Revision ID: e5fc52c21282
Revises: e31e63b8c2c0
Create Date: 2026-04-12 18:09:42.854763+00:00

The original migration created the experts table with admin-only RLS.
The /skills page now shows an Expert Agents tab to all authenticated
users, so the app role needs SELECT access.  The policy uses
USING (true) because experts are public catalog data — any session
on the app or admin role may read them.  Writes remain admin-only
via the existing experts_admin policy.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5fc52c21282"
down_revision: str | Sequence[str] | None = "e31e63b8c2c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE POLICY experts_read ON experts FOR SELECT USING (true)")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS experts_read ON experts")
