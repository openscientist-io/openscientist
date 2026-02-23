"""
User model for authentication and authorization.

The User model represents authenticated users in the system.
Users can log in via OAuth providers (GitHub, Google) and own jobs.
"""

from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .administrator import Administrator
    from .api_key import APIKey
    from .job import Job
    from .oauth_account import OAuthAccount
    from .session import Session


class User(UUIDv7Mixin, Base):
    """
    User account.

    Users authenticate via OAuth providers and own jobs. Multiple OAuth
    accounts can be linked to a single user (e.g., both GitHub and Google).

    Attributes:
        email: User's primary email address (unique, indexed)
        name: User's display name
        is_active: Whether the user account is active (for admin disable)
        is_approved: Whether the user is approved to start new jobs
        oauth_accounts: OAuth provider accounts linked to this user
        sessions: Active login sessions for this user
        api_keys: API keys created by this user
        jobs: Jobs owned by this user
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="User's primary email address",
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="User's display name",
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Whether user account is active",
    )

    is_approved: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="false",
        comment="Whether user is approved by an administrator to start jobs",
    )

    ntfy_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Whether push notifications via ntfy.sh are enabled",
    )

    ntfy_topic: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
        comment="Unique ntfy.sh topic for this user's notifications",
    )

    # Relationships
    oauth_accounts: Mapped[list["OAuthAccount"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    api_keys: Mapped[list["APIKey"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    jobs: Mapped[list["Job"]] = relationship(
        back_populates="owner",
    )

    administrator: Mapped[Optional["Administrator"]] = relationship(
        back_populates="user",
        uselist=False,
        foreign_keys="[Administrator.user_id]",
    )

    def __repr__(self) -> str:
        return f"<User(id={self.id}, email={self.email}, name={self.name})>"
