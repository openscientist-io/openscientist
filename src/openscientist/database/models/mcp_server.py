"""The admins' catalog of remote MCP servers."""

from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Text, UniqueConstraint, false
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, UUIDv7Mixin

#: The pattern a slug matches: lowercase letters, digits and inner hyphens.
MCP_SERVER_SLUG_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$"
#: The longest display name the table accepts, in characters.
MCP_SERVER_DISPLAY_NAME_MAX_LENGTH = 200
#: The longest URL the table accepts, in characters.
MCP_SERVER_URL_MAX_LENGTH = 2048


class McpServer(UUIDv7Mixin, Base):
    """One registered MCP server.

    A registration is catalog data only: no job reads the table, and the
    ``ck_mcp_servers_inactive`` check keeps every row inactive. Only the admin
    login may read the table or insert into it.
    """

    __tablename__ = "mcp_servers"

    __table_args__ = (
        UniqueConstraint("slug", name="uq_mcp_servers_slug"),
        CheckConstraint(f"slug ~ '{MCP_SERVER_SLUG_PATTERN}'", name="ck_mcp_servers_slug"),
        CheckConstraint(
            "display_name = btrim(display_name) AND char_length(display_name) "
            f"BETWEEN 1 AND {MCP_SERVER_DISPLAY_NAME_MAX_LENGTH}",
            name="ck_mcp_servers_display_name",
        ),
        CheckConstraint(
            r"url ~ '^https?://[^\s/?#@]+([/?#][^\s]*)?$' "
            f"AND char_length(url) <= {MCP_SERVER_URL_MAX_LENGTH}",
            name="ck_mcp_servers_url",
        ),
        CheckConstraint("NOT is_active", name="ck_mcp_servers_inactive"),
    )

    slug: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Unique identifier the admins chose"
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False, comment="Name shown to admins")
    url: Mapped[str] = mapped_column(Text, nullable=False, comment="HTTP(S) endpoint, never called")
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=false(),
        comment="Always false; no job uses a registered server",
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="Admin who registered it, NULL once deleted",
    )

    def __repr__(self) -> str:
        return f"<McpServer(slug={self.slug!r})>"
