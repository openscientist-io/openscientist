"""
MCP server for SHANDY tools.

Provides tools for autonomous discovery:
- execute_code: Run Python analysis on data
- search_pubmed: Search scientific literature
- update_knowledge_graph: Record findings
"""

from .server import main

__all__ = ["main"]
