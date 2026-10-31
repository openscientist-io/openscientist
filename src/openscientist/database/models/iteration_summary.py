"""Iteration summary model for per-iteration results."""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class IterationSummary(UUIDv7Mixin, Base):
    """Iteration summary."""

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

    # Relationships
    job: Mapped["Job"] = relationship()

    def __repr__(self) -> str:
        return f"<IterationSummary(id={self.id}, job_id={self.job_id}, iteration={self.iteration})>"
