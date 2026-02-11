"""
JobSkill junction model linking skills to jobs.

Tracks which skills are attached to each job, including whether they
were auto-matched and their relevance scores.
"""

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base

if TYPE_CHECKING:
    from .job import Job
    from .skill import Skill


class JobSkill(Base):
    """
    Junction table linking skills to jobs.

    Tracks which skills are associated with a job, whether they are
    enabled, and metadata about how they were matched.

    Attributes:
        job_id: Job that this skill is attached to
        skill_id: Skill attached to the job
        is_enabled: Whether this skill is enabled for the job
        relevance_score: Auto-match relevance score (0.0-1.0)
        match_reason: Explanation of why this skill was matched
        created_at: Timestamp when record was created
        job: Related Job object
        skill: Related Skill object
    """

    __tablename__ = "job_skills"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
        index=True,
        comment="Job that this skill is attached to",
    )

    skill_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
        index=True,
        comment="Skill attached to the job",
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Whether this skill is enabled for the job",
    )

    relevance_score: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Auto-match relevance score (0.0-1.0), NULL for manual selection",
    )

    match_reason: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Explanation of why this skill was matched",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
        comment="Timestamp when record was created (UTC)",
    )

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="job_skills")
    skill: Mapped["Skill"] = relationship(back_populates="job_skills")

    def __repr__(self) -> str:
        return (
            f"<JobSkill(job_id={self.job_id}, skill_id={self.skill_id}, "
            f"is_enabled={self.is_enabled}, relevance_score={self.relevance_score})>"
        )
