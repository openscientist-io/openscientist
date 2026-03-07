"""
Iteration summary model for per-iteration results.

Stores a summary of each analysis iteration including key findings,
decisions, and progress toward the goal.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class IterationSummary(UUIDv7Mixin, Base):
    """
    Iteration summary.

    Summarizes the results and decisions from a complete analysis iteration.
    Used for displaying progress and for the LLM to understand prior work.

    Attributes:
        job_id: Foreign key to the job this summary belongs to
        iteration: Iteration number being summarized
        summary_text: Natural language summary of iteration
        key_findings: List of important findings from this iteration
        hypotheses_generated: Number of new hypotheses generated
        hypotheses_tested: Number of hypotheses tested
        code_executed: Number of code blocks executed
        new_insights: List of new insights gained
        next_steps: Planned next steps
        metrics: Structured metrics (e.g., R-factors, completeness)
        job: Related Job object
    """

    __tablename__ = "iteration_summaries"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "iteration",
            name="uq_iteration_summaries_job_iteration",
        ),
    )

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this summary belongs to",
    )

    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Iteration number being summarized",
    )

    summary_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Natural language summary of iteration",
    )

    strapline: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Optional one-line headline for this iteration",
    )

    key_findings: Mapped[list[Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="List of important findings from this iteration",
    )

    hypotheses_generated: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of new hypotheses generated",
    )

    hypotheses_tested: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of hypotheses tested",
    )

    code_executed: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Number of code blocks executed",
    )

    new_insights: Mapped[list[Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="List of new insights gained",
    )

    next_steps: Mapped[list[Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Planned next steps",
    )

    metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Structured metrics (e.g., R-factors, completeness)",
    )

    # Relationships
    job: Mapped["Job"] = relationship()

    def __repr__(self) -> str:
        return f"<IterationSummary(id={self.id}, job_id={self.job_id}, iteration={self.iteration})>"
