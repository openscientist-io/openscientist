"""
Finding model for analysis discoveries.

Findings represent discrete analytical observations or results
from code execution, analysis, or data inspection.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .hypothesis import Hypothesis
    from .job import Job
    from .literature import Literature


class Finding(UUIDv7Mixin, Base):
    """
    Analysis finding.

    Represents a discrete analytical observation or result. Findings
    are generated through code execution, data inspection, or analysis
    and are linked to hypotheses they support or refute.

    Attributes:
        job_id: Foreign key to the job this finding belongs to
        iteration: Iteration number when finding was generated
        text: Finding description
        finding_type: Type of finding (observation/measurement/analysis/error)
        source: Source of finding (code_execution/literature/reasoning)
        data: Structured data associated with finding
        job: Related Job object (via hypothesis)
        hypotheses: Hypotheses this finding relates to (M2M)
        literature: Literature references (M2M)
    """

    __tablename__ = "findings"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this finding belongs to",
    )

    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Iteration number when finding was generated",
    )

    text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Finding description",
    )

    finding_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Type of finding (observation/measurement/analysis/error)",
    )

    source: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Source of finding (code_execution/literature/reasoning)",
    )

    data: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Structured data associated with finding",
    )

    # Relationships
    job: Mapped["Job"] = relationship()

    hypotheses: Mapped[list["Hypothesis"]] = relationship(
        secondary="finding_hypotheses",
        back_populates="findings",
    )

    literature: Mapped[list["Literature"]] = relationship(
        secondary="finding_literature",
        back_populates="findings",
    )

    def __repr__(self) -> str:
        return (
            f"<Finding(id={self.id}, iteration={self.iteration}, "
            f"type={self.finding_type}, text={self.text[:50]}...)>"
        )
