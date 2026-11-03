"""Move the slot end stamp out of the container, where it could stretch the run.

The web stamps ``jobs.ended_at`` on the end it observes, and the worker's
poll backfills the end the container wrote, so the agent's UPDATE grant on
the column is dead weight and its removal keeps a future container write
path from moving the fair-share totals.

Revision ID: slot_times_web_only
Revises: proxy_cost_metering
Create Date: 2026-11-03 13:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "slot_times_web_only"
down_revision: str | None = "proxy_cost_metering"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "openscientist_agent"


def upgrade() -> None:
    op.execute(f"REVOKE UPDATE (ended_at) ON jobs FROM {_ROLE}")


def downgrade() -> None:
    op.execute(f"GRANT UPDATE (ended_at) ON jobs TO {_ROLE}")
