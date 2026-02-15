"""
MCP configuration export utilities.

Generates MCP config JSON based on enabled MCP servers in the database.
Used for configuring custom MCP servers beyond the built-in shandy-tools.
"""

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import MCPServer

logger = logging.getLogger(__name__)


async def generate_mcp_config(
    session: AsyncSession,
    job_dir: Path,
) -> dict[str, Any]:
    """
    Generate MCP config JSON for a job.

    Reads all enabled MCP servers from database and generates
    a config dict with server definitions.

    Args:
        session: Database session
        job_dir: Job directory path (for server-specific config)

    Returns:
        MCP config dict with mcpServers definitions
    """
    # Get all enabled MCP servers
    stmt = (
        select(MCPServer)
        .where(MCPServer.is_enabled == True)  # noqa: E712
        .order_by(MCPServer.name)
    )
    result = await session.execute(stmt)
    servers = list(result.scalars().all())

    # Build MCP config
    mcp_servers: dict[str, dict[str, Any]] = {}

    for server in servers:
        server_config: dict[str, Any] = {
            "command": server.command,
        }

        # Process args - substitute placeholders
        args = list(server.args) if server.args else []
        processed_args = []
        for arg in args:
            if isinstance(arg, str):
                # Replace placeholders
                arg = arg.replace("${JOB_DIR}", str(job_dir))
                arg = arg.replace("${JOB_ID}", job_dir.name)
            processed_args.append(arg)
        server_config["args"] = processed_args

        # Process env - substitute placeholders
        env = dict(server.env) if server.env else {}
        processed_env = {}
        for key, value in env.items():
            if isinstance(value, str):
                value = value.replace("${JOB_DIR}", str(job_dir))
                value = value.replace("${JOB_ID}", job_dir.name)
            processed_env[key] = value
        if processed_env:
            server_config["env"] = processed_env

        mcp_servers[server.name] = server_config

    config = {"mcpServers": mcp_servers}

    logger.info("Generated MCP config with %d servers", len(mcp_servers))
    return config


def get_default_mcp_config(job_dir: Path) -> dict[str, Any]:
    """
    Get default MCP config for when no database servers are configured.

    This provides the shandy-tools server as a fallback.

    Args:
        job_dir: Job directory path

    Returns:
        Default MCP config dict
    """
    return {
        "mcpServers": {
            "shandy-tools": {
                "command": "python",
                "args": [
                    "-m",
                    "shandy.mcp_server",
                    "--job-dir",
                    str(job_dir),
                ],
                "env": {
                    "JOB_DIR": str(job_dir),
                    "PYTHONUNBUFFERED": "1",
                },
            }
        }
    }


async def write_mcp_config_file(
    session: AsyncSession,
    job_dir: Path,
    output_path: Path | None = None,
) -> Path:
    """
    Write MCP config to a JSON file.

    Args:
        session: Database session
        job_dir: Job directory path
        output_path: Path to write config (default: job_dir/mcp_config.json)

    Returns:
        Path to the written config file
    """
    if output_path is None:
        output_path = job_dir / "mcp_config.json"

    config = await generate_mcp_config(session, job_dir)

    # If no servers configured, use default
    if not config.get("mcpServers"):
        config = get_default_mcp_config(job_dir)

    output_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    logger.info("Wrote MCP config to %s", output_path)

    return output_path


def generate_mcp_config_sync(job_dir: Path) -> dict[str, Any]:
    """
    Generate MCP config synchronously (uses default config only).

    This is a convenience function for when async is not available.
    Uses the default shandy-tools server.

    Args:
        job_dir: Job directory path

    Returns:
        MCP config dict
    """
    return get_default_mcp_config(job_dir)
