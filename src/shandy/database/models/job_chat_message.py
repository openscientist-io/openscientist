"""
Job chat message model for in-page LLM chat.

Tracks chat messages between users and the LLM within a job's context.
The LLM has access to job data and knowledge state.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class JobChatMessage(UUIDv7Mixin, Base):
    """
    Chat message within a job.

    Enables users to chat with the LLM about analysis within the job context.
    The LLM has access to all job data, hypotheses, findings, and results.

    Attributes:
        job_id: Foreign key to the job this message belongs to
        role: Message role ('user' or 'assistant')
        content: Message text content
        job: Related Job object
    """

    __tablename__ = "job_chat_messages"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this message belongs to",
    )

    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Message role (user/assistant)",
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Message text content",
    )

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="chat_messages")

    def __repr__(self) -> str:
        preview = self.content[:50] + "..." if len(self.content) > 50 else self.content
        return f"<JobChatMessage(id={self.id}, role={self.role}, content={preview})>"
