"""Record how long the upstream took to answer each proxied Messages call.

Revision ID: llm_call_timings
Revises: monthly_project_spend
Create Date: 2026-11-18 13:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "llm_call_timings"
down_revision: str | None = "monthly_project_spend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE llm_call_timings (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
            job_id uuid REFERENCES jobs (id) ON DELETE SET NULL,
            cost_record_id uuid UNIQUE REFERENCES cost_records (id) ON DELETE SET NULL,
            run_kind varchar(32) NOT NULL,
            model varchar(100) NOT NULL,
            streamed boolean NOT NULL,
            status_code integer,
            headers_seconds double precision,
            first_event_seconds double precision,
            longest_event_gap_seconds double precision,
            duration_seconds double precision NOT NULL,
            input_tokens integer,
            output_tokens integer,
            ended varchar(32) NOT NULL CONSTRAINT ck_llm_call_timings_ended
                CHECK (ended IN ('complete', 'client_disconnected', 'upstream_ended_early'))
        )
        """
    )
    op.execute("CREATE INDEX ix_llm_call_timings_job_id ON llm_call_timings (job_id)")
    op.execute(
        "COMMENT ON TABLE llm_call_timings IS "
        "'How long the upstream took to answer each Messages call through the LLM proxy'"
    )
    # Only the admin session reads and writes the timings.
    op.execute("ALTER TABLE llm_call_timings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE llm_call_timings FORCE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON llm_call_timings FROM openscientist_app")
    op.execute("REVOKE ALL ON llm_call_timings FROM openscientist_agent")


def downgrade() -> None:
    op.execute("DROP TABLE llm_call_timings")
