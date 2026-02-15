"""
JobSkill junction model linking jobs to skills.

Tracks which skills were used for each job, including snapshot data
to preserve the skill content even if the skill is later deleted.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job
    from .skill import Skill


class JobSkill(UUIDv7Mixin, Base):
    """
    Association between a job and a skill.

    Records which skills were used for a job, either selected initially
    at job start or added by the agent during execution. Includes snapshot
    fields to preserve skill content even if the original skill is deleted.

    Attributes:
        job_id: Foreign key to the job
        skill_id: Foreign key to the skill (NULL if skill was deleted)
        skill_name: Snapshot of skill name at time of selection
        skill_category: Snapshot of skill category at time of selection
        skill_content: Snapshot of full skill content at time of selection
        source: How the skill was added ("initial" at job start, "agent" via MCP tool)
        job: Related Job object
        skill: Related Skill object (may be None if skill was deleted)
    """

    __tablename__ = "job_skills"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job that uses this skill",
    )

    skill_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("skills.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Original skill (NULL if skill was deleted)",
    )

    # Snapshot fields - preserved even if skill is deleted
    skill_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Skill name at time of selection",
    )

    skill_category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Skill category at time of selection",
    )

    skill_content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Full skill content at time of selection",
    )

    source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        server_default="initial",
        comment="How skill was added: 'initial' (job start) or 'agent' (via MCP tool)",
    )

    similarity_score: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="Relevance score from full-text search (0.0-1.0)",
    )

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="job_skills")
    skill: Mapped["Skill | None"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<JobSkill(id={self.id}, job_id={self.job_id}, "
            f"skill_name={self.skill_name}, source={self.source})>"
        )
