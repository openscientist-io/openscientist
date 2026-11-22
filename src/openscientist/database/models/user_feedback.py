"""Free-text feedback a user sends to the admins."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, UUIDv7Mixin

#: The longest message the table accepts, in characters.
FEEDBACK_MAX_LENGTH = 5000
#: How many messages one sender may send in any 24 hours.
FEEDBACK_DAILY_LIMIT = 10
#: The SQLSTATE the table raises for a message past the sender's daily limit.
FEEDBACK_LIMIT_SQLSTATE = "OSF01"


class UserFeedback(UUIDv7Mixin, Base):
    """One message a user sent, and whether an admin has read it.

    A user's session may only insert its own message, and at most
    ``FEEDBACK_DAILY_LIMIT`` in any 24 hours. Reading, marking and deleting go
    through the admin login.
    """

    __tablename__ = "user_feedback"

    user_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Sender, NULL once the sender's account is deleted",
    )
    message: Mapped[str] = mapped_column(Text, nullable=False, comment="The sender's text")
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When an admin marked it read"
    )
    read_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Admin who marked it read",
    )

    def __repr__(self) -> str:
        return f"<UserFeedback(id={self.id}, read={self.read_at is not None})>"
