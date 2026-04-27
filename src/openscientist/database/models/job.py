"""
Job model for scientific analysis jobs.

The Job model represents a complete OpenScientist analysis workflow, tracking
status, configuration, and relationships to all job artifacts.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .cost_record import CostRecord
    from .hypothesis import Hypothesis
    from .job_chat_message import JobChatMessage
    from .job_data_file import JobDataFile
    from .job_share import JobShare
    from .user import User


class Job(UUIDv7Mixin, Base):
    """
    Scientific analysis job.

    Represents a complete OpenScientist workflow from data upload through
    iterative analysis to final report generation.

    Attributes:
        owner_id: Foreign key to user who owns this job (NULL for orphaned)
        research_question: Research question that drives the agent prompt
        short_title: Optional short display label (model- or user-generated)
        description: User-provided job description
        investigation_mode: Investigation mode (autonomous/coinvestigate)
        status: Current job status (pending/running/completed/failed/cancelled)
        max_iterations: Maximum number of analysis iterations allowed
        current_iteration: Current iteration number (0-based)
        pdb_code: PDB code if analyzing existing structure
        space_group: Crystal space group
        resume_iteration: Iteration to resume from (NULL for new jobs)
        llm_provider: LLM provider being used (vertex/bedrock/cborg)
        llm_config: LLM configuration (model, temperature, etc.)
        error_message: Error message if job failed
        result_summary: Final analysis summary
        owner: Related User object
        shares: Job sharing permissions
        data_files: Uploaded data files for this job
        hypotheses: Generated hypotheses
        chat_messages: In-page chat messages
        cost_records: Cost tracking records
    """

    __tablename__ = "jobs"

    owner_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="User who owns this job (NULL for orphaned legacy jobs)",
    )

    research_question: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Research question that drives the agent prompt",
    )

    short_title: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Optional short display label (model- or user-generated)",
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="User-provided job description",
    )

    use_hypotheses: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="Whether hypothesis tracking tools are enabled",
    )

    investigation_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        server_default="autonomous",
        comment="Investigation mode (autonomous or coinvestigate)",
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        server_default="pending",
        comment="Current job status (pending/running/completed/failed/cancelled)",
    )

    max_iterations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="5",
        comment="Maximum number of analysis iterations allowed",
    )

    current_iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="Current iteration number (0-based)",
    )

    pdb_code: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="PDB code if analyzing existing structure",
    )

    space_group: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Crystal space group",
    )

    resume_iteration: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Iteration to resume from (NULL for new jobs)",
    )

    llm_provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="vertex",
        comment="LLM provider being used (vertex/bedrock/cborg)",
    )

    llm_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="LLM configuration (model, temperature, etc.)",
    )

    data_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Structured data summary used for prompting and UI",
    )

    agent_status: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Current agent status message for live UI updates",
    )

    agent_status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when agent_status was last updated",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error message if job failed",
    )

    cancellation_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Reason why the job was cancelled",
    )

    result_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Final analysis summary",
    )

    consensus_answer: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Consensus answer to the research question if one was reached",
    )

    # Relationships
    owner: Mapped["User | None"] = relationship(back_populates="jobs")

    shares: Mapped[list["JobShare"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    data_files: Mapped[list["JobDataFile"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    hypotheses: Mapped[list["Hypothesis"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    chat_messages: Mapped[list["JobChatMessage"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    cost_records: Mapped[list["CostRecord"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        snippet = self.research_question[:50] if self.research_question else ""
        return (
            f"<Job(id={self.id}, research_question={snippet!r}, status={self.status}, "
            f"owner_id={self.owner_id})>"
        )
