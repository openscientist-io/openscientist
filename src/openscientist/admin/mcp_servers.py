"""Registering remote MCP servers in the admins' catalog.

A registration stores a slug, a display name and an HTTP(S) URL, and nothing
more. Nothing here contacts the server, and every registration is inactive.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import McpServer
from openscientist.database.models.mcp_server import (
    MCP_SERVER_DISPLAY_NAME_MAX_LENGTH,
    MCP_SERVER_SLUG_PATTERN,
    MCP_SERVER_URL_MAX_LENGTH,
)

logger = logging.getLogger(__name__)

_SLUG = re.compile(MCP_SERVER_SLUG_PATTERN)
_HTTP_URL: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)
_INVALID_URL = "URL is not a valid HTTP(S) URL"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _has_blank_or_control(value: str) -> bool:
    return any(char.isspace() or not char.isprintable() for char in value)


class McpServerRegistration(BaseModel):
    """A validated registration. The URL is stored as the HTTP(S) parser
    normalised it, so later readers see one unambiguous form."""

    model_config = ConfigDict(frozen=True)

    slug: str
    display_name: str
    url: str

    @field_validator("slug", mode="before")
    @classmethod
    def _check_slug(cls, value: Any) -> str:
        slug = _text(value).strip()
        if not slug:
            raise ValueError("Slug is required")
        if not _SLUG.fullmatch(slug):
            raise ValueError(
                "Slug must be 1 to 64 lowercase letters, digits or hyphens, "
                "starting and ending with a letter or digit"
            )
        return slug

    @field_validator("display_name", mode="before")
    @classmethod
    def _check_display_name(cls, value: Any) -> str:
        name = _text(value).strip()
        if not name:
            raise ValueError("Display name is required")
        if len(name) > MCP_SERVER_DISPLAY_NAME_MAX_LENGTH:
            raise ValueError(
                f"Display name must be at most {MCP_SERVER_DISPLAY_NAME_MAX_LENGTH} characters"
            )
        if any(not char.isprintable() for char in name):
            raise ValueError("Display name must not contain line breaks or control characters")
        return name

    @field_validator("url", mode="before")
    @classmethod
    def _check_url(cls, value: Any) -> str:
        raw = _text(value)
        if not raw:
            raise ValueError("URL is required")
        if _has_blank_or_control(raw):
            raise ValueError("URL must not contain spaces, line breaks or control characters")
        if len(raw) > MCP_SERVER_URL_MAX_LENGTH:
            raise ValueError(f"URL must be at most {MCP_SERVER_URL_MAX_LENGTH} characters")
        try:
            parts = urlsplit(raw)
        except ValueError:
            raise ValueError(_INVALID_URL) from None
        if parts.scheme.lower() not in ("http", "https"):
            raise ValueError("URL must start with http:// or https://")
        # urlsplit sees no host in "http:///path", which the HTTP(S) parser
        # below would read as host "path".
        if not parts.netloc or not parts.hostname:
            raise ValueError("URL must name a host")
        if "@" in parts.netloc:
            raise ValueError("URL must not contain a user name or password")
        try:
            parsed = _HTTP_URL.validate_python(raw)
        except ValidationError:
            raise ValueError(_INVALID_URL) from None
        if not parsed.host:
            raise ValueError("URL must name a host")
        if parsed.username or parsed.password:
            raise ValueError("URL must not contain a user name or password")
        normalized = str(parsed)
        if len(normalized) > MCP_SERVER_URL_MAX_LENGTH:
            raise ValueError(f"URL must be at most {MCP_SERVER_URL_MAX_LENGTH} characters")
        return normalized


def parse_registration(slug: Any, display_name: Any, url: Any) -> McpServerRegistration | list[str]:
    """The validated registration, or one message for each field that is wrong."""
    try:
        return McpServerRegistration(slug=slug, display_name=display_name, url=url)
    except ValidationError as error:
        messages = []
        for detail in error.errors():
            cause = detail.get("ctx", {}).get("error")
            messages.append(str(cause) if cause is not None else detail["msg"])
        return messages


async def list_mcp_servers(session: AsyncSession) -> list[McpServer]:
    """Every registered server, by slug."""
    result = await session.execute(select(McpServer).order_by(McpServer.slug))
    return list(result.scalars())


async def register_mcp_server(
    session: AsyncSession, registration: McpServerRegistration, created_by: UUID
) -> tuple[bool, str]:
    """Add ``registration`` to the catalog as inactive, recorded as registered
    by ``created_by``. A taken slug, even one registered concurrently, adds
    nothing.

    Returns:
        Tuple of (success, message)
    """
    try:
        inserted = await session.scalar(
            insert(McpServer)
            .values(
                slug=registration.slug,
                display_name=registration.display_name,
                url=registration.url,
                created_by=created_by,
            )
            .on_conflict_do_nothing(constraint="uq_mcp_servers_slug")
            .returning(McpServer.id)
        )
    except IntegrityError:
        await session.rollback()
        logger.warning("The database refused the MCP server %r", registration.slug, exc_info=True)
        return False, "The MCP server could not be registered"
    if inserted is None:
        await session.rollback()
        return False, f'An MCP server with the slug "{registration.slug}" is already registered'
    await session.commit()
    return True, f'Registered "{registration.display_name}" as inactive'
