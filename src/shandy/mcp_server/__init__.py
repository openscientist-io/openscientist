"""
MCP server for SHANDY tools.

Provides tools for autonomous discovery:
- execute_code: Run Python analysis on data
- search_pubmed: Search scientific literature
- update_knowledge_state: Record findings
"""

from shandy.mcp_server.server import main

__all__ = ["main"]
