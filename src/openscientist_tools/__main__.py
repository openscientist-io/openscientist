"""Stdio entry point: ``python -m openscientist_tools``."""

from __future__ import annotations

from openscientist_tools.server import mcp

if __name__ == "__main__":
    mcp.run()
