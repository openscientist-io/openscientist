"""
Agent package for OpenScientist.

Provides the ClaudeCodeAgent implementation (backed by claude-agent-sdk).
Use get_agent() from agent.factory to get the agent for the configured
provider.
"""

from openscientist.agent.base import IterationResult, TokenUsage, TurnOutcome
from openscientist.agent.mcp_specs import (
    HttpMcpServerSpec,
    McpServerSpec,
    StdioMcpServerSpec,
)

__all__ = [
    "HttpMcpServerSpec",
    "IterationResult",
    "McpServerSpec",
    "StdioMcpServerSpec",
    "TokenUsage",
    "TurnOutcome",
]
