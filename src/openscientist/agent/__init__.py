"""
Agent executor package for OpenScientist.

Provides the AgentExecutor protocol and the SDKAgentExecutor implementation
(backed by claude-agent-sdk).  Use get_agent_executor() from agent.factory
to get the executor.
"""

from openscientist.agent.protocol import AgentExecutor, IterationResult, TokenUsage

__all__ = ["AgentExecutor", "IterationResult", "TokenUsage"]
