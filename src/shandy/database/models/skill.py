"""
Skill model for domain-specific analysis capabilities.

Skills are markdown documents with YAML frontmatter that provide specialized
knowledge and workflows for scientific analysis tasks.
"""

from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .skill_source import SkillSource


class Skill(UUIDv7Mixin, Base):
    """
    Domain-specific skill for scientific analysis.

    Skills provide specialized knowledge and workflows that can be
    auto-matched to jobs based on research questions. They are stored
    as markdown with YAML frontmatter and support full-text search.

    Attributes:
        name: Human-readable skill name
        slug: URL-friendly identifier (unique within category)
        category: Skill category (e.g., metabolomics, genomics)
        description: Brief description of what the skill does
        content: Full markdown content of the skill
        tags: Array of tags for filtering and search
        search_vector: PostgreSQL tsvector for full-text search
        source_id: Skill source this skill was synced from
        source_path: Path within source where skill was found
        content_hash: SHA256 hash of content for change detection
        commit_sha: Git commit SHA when skill was synced
        is_enabled: Whether this skill is available for use
        version: Version number for tracking updates
        source: Related SkillSource object
    """

    __tablename__ = "skills"

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Human-readable skill name",
    )

    slug: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
        comment="URL-friendly identifier (unique within category)",
    )

    category: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        index=True,
        comment="Skill category (e.g., metabolomics, genomics, data-science)",
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Brief description of what the skill does",
    )

    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Full markdown content of the skill",
    )

    tags: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
        comment="Array of tags for filtering and search",
    )

    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        nullable=True,
        comment="Full-text search vector (auto-generated)",
    )

    source_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("skill_sources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Skill source this skill was synced from (NULL for built-in)",
    )

    source_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Path within source where skill was found",
    )

    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA256 hash of content for change detection",
    )

    commit_sha: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Git commit SHA when skill was synced",
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        index=True,
        comment="Whether this skill is available for use",
    )

    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="1",
        comment="Version number for tracking updates",
    )

    # Relationships
    source: Mapped["SkillSource | None"] = relationship(back_populates="skills")

    def __repr__(self) -> str:
        return (
            f"<Skill(id={self.id}, name={self.name}, category={self.category}, slug={self.slug})>"
        )
