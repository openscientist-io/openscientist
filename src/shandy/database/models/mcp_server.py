"""
MCP server configuration model.

Stores MCP server definitions that can be enabled/disabled by admins
and automatically included in agent containers.
"""

from sqlalchemy import Boolean, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, UUIDv7Mixin


class MCPServer(UUIDv7Mixin, Base):
    """
    MCP server configuration for Claude agent containers.

    Defines MCP servers that are available to the Claude agent. All enabled
    MCP servers are included in agent containers - there is no per-job
    selection.

    Attributes:
        name: Short identifier for the server (e.g., "shandy-tools")
        description: Human-readable description of what this server provides
        command: Command to run the server (e.g., "python", "node")
        args: Arguments to pass to the command (JSONB array)
        env: Environment variables for the server (JSONB object)
        is_enabled: Whether this server is active
    """

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="Short identifier for the server (e.g., shandy-tools)",
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable description of server capabilities",
    )

    command: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Command to run the server (e.g., python, node)",
    )

    args: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        server_default="[]",
        comment="Arguments to pass to the command",
    )

    env: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default="{}",
        comment="Environment variables for the server",
    )

    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default="true",
        index=True,
        comment="Whether this server is active",
    )

    def __repr__(self) -> str:
        return f"<MCPServer(id={self.id}, name={self.name}, is_enabled={self.is_enabled})>"
