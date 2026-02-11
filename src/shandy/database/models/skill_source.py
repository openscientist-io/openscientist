"""
SkillSource model for external skill repositories.

Tracks external sources (e.g., GitHub repos) from which skills can be synced.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .skill import Skill


class SkillSource(UUIDv7Mixin, Base):
    """
    External source for syncing skills.

    Represents a repository or directory from which skills can be imported
    and kept in sync. Supports GitHub repos and local filesystem paths.

    Attributes:
        source_type: Type of source (github/local)
        name: Human-readable name for the source
        url: URL for remote sources (e.g., GitHub repo URL)
        path: Local filesystem path for local sources
        branch: Git branch to sync from (for GitHub sources)
        skills_path: Subdirectory within repo containing skills
        last_synced_at: Timestamp of last successful sync
        last_commit_sha: Last synced Git commit SHA
        is_enabled: Whether this source is enabled for syncing
        sync_error: Error message from last failed sync attempt
        skills: Skills imported from this source
    """

    __tablename__ = "skill_sources"

    source_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
        comment="Type of source (github/local)",
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable name for the source",
    )

    url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="URL for remote sources (e.g., GitHub repo URL)",
    )

    path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Local filesystem path for local sources",
    )

    branch: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        server_default="main",
        comment="Git branch to sync from (for GitHub sources)",
    )

    skills_path: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        server_default="",
        comment="Subdirectory within repo containing skills",
    )

    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp of last successful sync",
    )

    last_commit_sha: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Last synced Git commit SHA",
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        comment="Whether this source is enabled for syncing",
    )

    sync_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Error message from last failed sync attempt",
    )

    # Relationships
    skills: Mapped[list["Skill"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<SkillSource(id={self.id}, name={self.name}, "
            f"source_type={self.source_type})>"
        )
