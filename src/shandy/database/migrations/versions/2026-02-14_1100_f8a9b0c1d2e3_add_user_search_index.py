"""Add GIN trigram index for user search.

This migration adds a combined GIN index with pg_trgm (trigram) extension
for efficient partial/fuzzy text search on user email and name fields.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-02-14 11:00:00.000000

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add GIN trigram indexes for user search."""
    # Enable pg_trgm extension for trigram similarity search
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Add GIN trigram index on email for partial matching (e.g., "john@" matches "john@example.com")
    # Note: Not using CONCURRENTLY as it cannot run inside a transaction
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_users_email_trgm
        ON users USING gin (email gin_trgm_ops)
        """
    )

    # Add GIN trigram index on name for partial matching (e.g., "joh" matches "John Doe")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_users_name_trgm
        ON users USING gin (name gin_trgm_ops)
        """
    )


def downgrade() -> None:
    """Remove GIN trigram indexes."""
    op.execute("DROP INDEX IF EXISTS ix_users_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_users_email_trgm")
    # Note: We don't drop the pg_trgm extension as it may be used by other indexes
