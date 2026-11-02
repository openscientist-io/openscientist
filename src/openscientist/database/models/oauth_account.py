"""OAuth account model for linked authentication providers."""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin
from ..types import EncryptedText

if TYPE_CHECKING:
    from .user import User


class OAuthAccount(UUIDv7Mixin, Base):
    """OAuth provider account linked to a user."""

    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_user_id",
            name="uq_oauth_provider_user",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="User who owns this OAuth account",
    )

    provider: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="OAuth provider name (github, google, orcid)",
    )

    provider_user_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="User's unique ID on the OAuth provider",
    )

    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Email address from OAuth provider",
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Display name from OAuth provider",
    )

    access_token: Mapped[str | None] = mapped_column(
        EncryptedText(),
        nullable=True,
        comment="OAuth access token (encrypted at rest)",
    )

    refresh_token: Mapped[str | None] = mapped_column(
        EncryptedText(),
        nullable=True,
        comment="OAuth refresh token (encrypted at rest)",
    )

    # Relationships
    user: Mapped["User"] = relationship(back_populates="oauth_accounts")

    def __repr__(self) -> str:
        return f"<OAuthAccount(id={self.id}, provider={self.provider}, email={self.email})>"
