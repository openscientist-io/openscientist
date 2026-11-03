"""Stop the agent writing cost rows, now that the LLM proxy meters every call.

The web's proxy observes the usage on the wire and writes cost_records through
its own login, so the agent's INSERT grant is dead weight and its removal
closes the door a future grant or function-owner mistake would otherwise keep
open. The CHECK constraint is the second lock, because the agent keeps its
row-scoped SELECT and a wrong negative row would move the budget totals.

Revision ID: proxy_cost_metering
Revises: job_is_public
Create Date: 2026-11-03 12:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "proxy_cost_metering"
down_revision: str | None = "job_is_public"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE = "openscientist_agent"
_CONSTRAINT = "cost_records_non_negative"

_NON_NEGATIVE = (
    "cost_usd >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
    "AND cache_read_tokens >= 0 AND cache_write_tokens >= 0 AND reasoning_tokens >= 0"
)


def upgrade() -> None:
    op.execute(f"ALTER TABLE cost_records ADD CONSTRAINT {_CONSTRAINT} CHECK ({_NON_NEGATIVE})")
    op.execute(f"REVOKE INSERT ON cost_records FROM {_ROLE}")


def downgrade() -> None:
    op.execute(f"ALTER TABLE cost_records DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(f"GRANT INSERT ON cost_records TO {_ROLE}")
