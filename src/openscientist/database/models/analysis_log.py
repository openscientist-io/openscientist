"""Analysis log model for tracking analysis steps."""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class AnalysisLog(UUIDv7Mixin, Base):
    """Analysis log entry."""

    __tablename__ = "analysis_log"
    # The key an output row references, so its job is the log's own.
    __table_args__ = (UniqueConstraint("id", "job_id", name="uq_analysis_log_id_job_id"),)

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
    output: Mapped["AnalysisLogOutput | None"] = relationship(
        back_populates="analysis_log", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return (
            f"<AnalysisLog(id={self.id}, iteration={self.iteration}, "
            f"step={self.step_number}, action={self.action_type})>"
        )


class AnalysisLogOutput(Base):
    """Raw action output visible to the job's owner and share recipients."""

    __tablename__ = "analysis_log_outputs"
    # The output names its log together with that log's job, so a row can never
    # pair one job's id with another job's log.
    __table_args__ = (
        ForeignKeyConstraint(
            ["analysis_log_id", "job_id"],
            ["analysis_log.id", "analysis_log.job_id"],
            ondelete="CASCADE",
            name="analysis_log_outputs_log_fkey",
        ),
    )

    analysis_log_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    output: Mapped[str] = mapped_column(Text, nullable=False)

    analysis_log: Mapped[AnalysisLog] = relationship(back_populates="output")
