"""
Session model for user login sessions.

Tracks active user sessions created after successful OAuth login.
Sessions expire after a configurable period of inactivity.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .user import User


class Session(UUIDv7Mixin, Base):
    """
    User login session.

    Created after successful OAuth login. The session ID is stored in a
    secure HTTP-only cookie. Sessions expire after inactivity.

    Attributes:
        user_id: Foreign key to the user who owns this session
        expires_at: Timestamp when the session expires (UTC)
        user: Related User object
    """

    __tablename__ = "sessions"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User who owns this session",
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        comment="Timestamp when session expires (UTC)",
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="sessions")

    def __repr__(self) -> str:
        return f"<Session(id={self.id}, user_id={self.user_id}, expires_at={self.expires_at})>"

    def is_expired(self) -> bool:
        """Check if the session has expired."""

        return datetime.now(UTC) > self.expires_at
