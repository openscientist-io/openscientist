"""Specs describing how to launch an external MCP server.

Used to construct ``claude-agent-sdk`` MCP server config entries for
``ClaudeAgentOptions.mcp_servers``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from openscientist.settings import Settings

logger = logging.getLogger(__name__)

OPENADMODELS_MCP_SERVER_NAME = "openadmodels"


class StdioMcpServerSpec(BaseModel):
    """How to spawn an MCP server over stdio."""

    model_config = ConfigDict(frozen=True)

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    cwd: str | None = None

    def to_sdk_config(self) -> dict[str, Any]:
        """Return the ``claude-agent-sdk`` ``McpStdioServerConfig`` dict.

        ``cwd`` is intentionally omitted. The SDK config has no place for it, and
        the launcher reads ``spec.cwd`` directly when spawning the subprocess.
        """
        cfg: dict[str, Any] = {"type": "stdio", "command": self.command}
        if self.args:
            cfg["args"] = list(self.args)
        if self.env:
            cfg["env"] = dict(self.env)
        return cfg


class HttpMcpServerSpec(BaseModel):
    """How to reach an MCP server over HTTP."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str
    headers: dict[str, str] | None = None

    def to_sdk_config(self) -> dict[str, Any]:
        """Return the `claude-agent-sdk` `McpHttpServerConfig` dict."""
        cfg: dict[str, Any] = {"type": "http", "url": self.url}
        if self.headers:
            cfg["headers"] = dict(self.headers)
        return cfg


McpServerSpec = StdioMcpServerSpec | HttpMcpServerSpec


def build_openadmodels_mcp_spec(
    settings: Settings, *, log: logging.Logger = logger
) -> HttpMcpServerSpec | None:
    """Return the OpenADModels HTTP MCP server spec when configured and not air-gapped."""
    if not settings.openadmodels.is_available:
        return None
    if settings.airgap.enabled:
        log.info("Skipping OpenADModels MCP server: air-gapped mode is enabled")
        return None
    assert settings.openadmodels.mcp_url is not None
    headers = (
        {"Authorization": f"Bearer {settings.openadmodels.mcp_key}"}
        if settings.openadmodels.mcp_key
        else None
    )
    return HttpMcpServerSpec(
        name=OPENADMODELS_MCP_SERVER_NAME,
        url=settings.openadmodels.mcp_url,
        headers=headers,
    )
