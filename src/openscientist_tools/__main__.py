"""Stdio entry point: ``python -m openscientist_tools``."""

from __future__ import annotations

from openscientist_tools.server import _apply_airgap_policy, mcp

if __name__ == "__main__":
    _apply_airgap_policy()
    mcp.run()
