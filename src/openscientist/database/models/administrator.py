"""Administrator model for admin access control."""

from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from ..base import Base

if TYPE_CHECKING:
    from .user import User


class Administrator(Base):
    """Administrator record linking a user to admin privileges.

    A separate table rather than a boolean on User, to keep an audit trail of
    who granted what and when.
    """

    __tablename__ = "administrators"

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="User who has admin privileges",
    )

    granted_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User who granted admin privileges (NULL for initial setup)",
    )

    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        nullable=False,
        comment="Timestamp when admin was granted (UTC)",
    )

    notes: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Optional notes about why admin was granted",
    )

    # Relationships
    user: Mapped["User"] = relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="administrator",
    )

    granter: Mapped[Optional["User"]] = relationship(
        "User",
        foreign_keys=[granted_by],
    )

    def __repr__(self) -> str:
        return f"<Administrator(user_id={self.user_id}, granted_at={self.granted_at})>"
