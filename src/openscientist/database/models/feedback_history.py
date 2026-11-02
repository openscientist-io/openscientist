"""Feedback history model for user feedback."""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class FeedbackHistory(UUIDv7Mixin, Base):
    """User feedback entry."""

    __tablename__ = "feedback_history"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this feedback belongs to",
    )

    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Iteration number when feedback was provided",
    )

    feedback_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Type of feedback (correction/suggestion/question/approval)",
    )

    feedback_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="User's feedback text",
    )

    applied: Mapped[bool] = mapped_column(
        nullable=False,
        server_default="false",
        comment="Whether feedback has been incorporated",
    )

    # Relationships
    job: Mapped["Job"] = relationship()

    def __repr__(self) -> str:
        return (
            f"<FeedbackHistory(id={self.id}, iteration={self.iteration}, "
            f"type={self.feedback_type})>"
        )
