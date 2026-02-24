"""
Analysis log model for tracking analysis steps.

Records each step in the analysis workflow for transparency,
debugging, and reproducibility.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class AnalysisLog(UUIDv7Mixin, Base):
    """
    Analysis log entry.

    Records each step in the analysis workflow including decisions,
    actions taken, code executed, and results.

    Attributes:
        job_id: Foreign key to the job this log entry belongs to
        iteration: Iteration number when entry was created
        step_number: Sequential step number within iteration
        action_type: Type of action (reasoning/code_execution/literature_search/etc)
        description: Human-readable description of the action
        input_data: Input data for this step
        output_data: Output/result data from this step
        duration_seconds: Time taken for this step
        success: Whether the step completed successfully
        error_message: Error message if step failed
        job: Related Job object
    """

    __tablename__ = "analysis_log"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this log entry belongs to",
    )

    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Iteration number when entry was created",
    )

    step_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Sequential step number within iteration",
    )

    action_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Type of action (reasoning/code_execution/literature_search/etc)",
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Human-readable description of the action",
    )

    input_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Input data for this step",
    )

    output_data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Output/result data from this step",
    )

    duration_seconds: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="Time taken for this step",
    )

    success: Mapped[bool] = mapped_column(
        nullable=False,
        server_default="true",
        comment="Whether the step completed successfully",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if step failed",
    )

    # Relationships
    job: Mapped["Job"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<AnalysisLog(id={self.id}, iteration={self.iteration}, "
            f"step={self.step_number}, action={self.action_type})>"
        )
