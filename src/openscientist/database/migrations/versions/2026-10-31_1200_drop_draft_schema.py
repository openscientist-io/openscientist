"""Drop the draft schema that nothing writes or reads.

The ``hypothesis_spawns`` table and these columns were part of the first schema
draft, and no code has written or read them since: ``jobs.resume_iteration``,
``sessions.ip_address`` and ``user_agent``, seven ``iteration_summaries``
columns, ``literature.journal``, ``doi``, ``relevance_score`` and
``citation_key``, ``feedback_history.response_text``, and ``plots.plot_type``
and ``code_used``. The downgrade restores them empty, with the agent's update
grant on ``literature.journal`` and ``doi`` and the table's row security.

Revision ID: drop_draft_schema
Revises: user_priority
Create Date: 2026-10-31 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "drop_draft_schema"
down_revision: str | None = "user_priority"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _columns() -> tuple[tuple[str, tuple[sa.Column, ...]], ...]:
    """Each table, then its dropped columns as the first schema created them."""
    return (
        (
            "jobs",
            (
                sa.Column(
                    "resume_iteration",
                    sa.Integer(),
                    nullable=True,
                    comment="Iteration to resume from (NULL for new jobs)",
                ),
            ),
        ),
        (
            "sessions",
            (
                sa.Column(
                    "ip_address",
                    sa.String(length=45),
                    nullable=True,
                    comment="IP address where session was created",
                ),
                sa.Column(
                    "user_agent", sa.Text(), nullable=True, comment="Browser user agent string"
                ),
            ),
        ),
        (
            "iteration_summaries",
            (
                sa.Column(
                    "key_findings",
                    _jsonb(),
                    nullable=True,
                    comment="List of important findings from this iteration",
                ),
                sa.Column(
                    "hypotheses_generated",
                    sa.Integer(),
                    nullable=True,
                    comment="Number of new hypotheses generated",
                ),
                sa.Column(
                    "hypotheses_tested",
                    sa.Integer(),
                    nullable=True,
                    comment="Number of hypotheses tested",
                ),
                sa.Column(
                    "code_executed",
                    sa.Integer(),
                    nullable=True,
                    comment="Number of code blocks executed",
                ),
                sa.Column(
                    "new_insights", _jsonb(), nullable=True, comment="List of new insights gained"
                ),
                sa.Column("next_steps", _jsonb(), nullable=True, comment="Planned next steps"),
                sa.Column(
                    "metrics",
                    _jsonb(),
                    nullable=True,
                    comment="Structured metrics (e.g., R-factors, completeness)",
                ),
            ),
        ),
        (
            "literature",
            (
                sa.Column(
                    "journal", sa.Text(), nullable=True, comment="Journal or publication venue"
                ),
                sa.Column(
                    "doi", sa.String(length=255), nullable=True, comment="Digital Object Identifier"
                ),
                sa.Column(
                    "relevance_score",
                    sa.Float(),
                    nullable=True,
                    comment="Relevance to current analysis (0.0-1.0)",
                ),
                sa.Column(
                    "citation_key",
                    sa.String(length=100),
                    nullable=True,
                    comment="BibTeX citation key",
                ),
            ),
        ),
        (
            "feedback_history",
            (
                sa.Column(
                    "response_text",
                    sa.Text(),
                    nullable=True,
                    comment="LLM's response to the feedback",
                ),
            ),
        ),
        (
            "plots",
            (
                sa.Column(
                    "plot_type",
                    sa.String(length=50),
                    nullable=True,
                    comment="Type of plot (histogram/scatter/heatmap/etc)",
                ),
                sa.Column(
                    "code_used",
                    sa.Text(),
                    nullable=True,
                    comment="Python code that generated the plot",
                ),
            ),
        ),
    )


_SPAWN_VISIBLE_THROUGH = """
    EXISTS (
        SELECT 1 FROM hypotheses
        JOIN jobs ON jobs.id = hypotheses.job_id
        WHERE hypotheses.id = hypothesis_spawns.{column}
        AND (
            jobs.owner_id::text = current_setting('app.current_user_id', TRUE)
            OR EXISTS (
                SELECT 1 FROM job_shares
                WHERE job_shares.job_id = jobs.id
                AND job_shares.shared_with_user_id::text = current_setting('app.current_user_id', TRUE)
            )
        )
    )
"""


def upgrade() -> None:
    op.drop_table("hypothesis_spawns")
    op.drop_index("ix_literature_doi", table_name="literature")
    for table, columns in _columns():
        for column in columns:
            op.drop_column(table, column.name)


def downgrade() -> None:
    for table, columns in _columns():
        for column in columns:
            op.add_column(table, column)
    op.create_index("ix_literature_doi", "literature", ["doi"], unique=False)
    op.execute("GRANT UPDATE (journal, doi) ON literature TO openscientist_agent")

    op.create_table(
        "hypothesis_spawns",
        sa.Column(
            "parent_id",
            sa.UUID(),
            nullable=False,
            comment="Parent hypothesis that spawned the child",
        ),
        sa.Column(
            "child_id", sa.UUID(), nullable=False, comment="Child hypothesis that was spawned"
        ),
        sa.Column(
            "spawn_reason",
            sa.Text(),
            nullable=True,
            comment="Explanation for why child was spawned",
        ),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("uuidv7()"),
            nullable=False,
            comment="UUIDv7 primary key generated by PostgreSQL",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="Timestamp when record was created (UTC)",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="Timestamp when record was last updated (UTC)",
        ),
        sa.ForeignKeyConstraint(["child_id"], ["hypotheses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["hypotheses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hypothesis_spawns_child_id", "hypothesis_spawns", ["child_id"])
    op.create_index("ix_hypothesis_spawns_parent_id", "hypothesis_spawns", ["parent_id"])
    op.execute("ALTER TABLE hypothesis_spawns ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE hypothesis_spawns FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY hypothesis_spawns_all ON hypothesis_spawns TO openscientist_app USING ("
        + _SPAWN_VISIBLE_THROUGH.format(column="parent_id")
        + " OR "
        + _SPAWN_VISIBLE_THROUGH.format(column="child_id")
        + ")"
    )
