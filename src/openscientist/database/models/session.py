"""Session model for user login sessions."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .user import User


def hash_session_token(token: str) -> str:
    """Hash a session cookie before storing or looking it up."""
    return hashlib.sha256(token.encode()).hexdigest()


class Session(UUIDv7Mixin, Base):
    """User login session."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    @classmethod
    def issue(cls, *, user_id: UUID, expires_at: datetime) -> tuple[Session, str]:
        """Return a session row and its one-time plaintext cookie."""
        token = secrets.token_urlsafe(32)
        return cls(
            user_id=user_id, expires_at=expires_at, token_hash=hash_session_token(token)
        ), token

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
        return datetime.now(UTC) > self.expires_at
