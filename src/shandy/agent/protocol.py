"""
AgentExecutor protocol definition for SHANDY.

Defines the interface that all agent executors must implement,
allowing the orchestrator to be decoupled from any specific provider
or execution strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class TokenUsage:
    """Token usage accounting across all iterations."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )

    def __iadd__(self, other: TokenUsage) -> TokenUsage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        return self


@dataclass(frozen=True)
class IterationResult:
    """Result of a single agent iteration."""

    success: bool
    output: str
    tool_calls: int
    transcript: list[dict[str, Any]]
    error: str = ""


@runtime_checkable
class AgentExecutor(Protocol):
    """
    Protocol for agent executors.

    All executors must implement run_iteration, shutdown, and total_tokens.
    This protocol is @runtime_checkable, enabling isinstance() checks in tests.

    Usage::

        executor: AgentExecutor = get_agent_executor(...)
        result = await executor.run_iteration(prompt, reset_session=True)
        print(result.output)
        await executor.shutdown()
    """

    async def run_iteration(
        self,
        prompt: str,
        *,
        reset_session: bool = False,
    ) -> IterationResult:
        """
        Run a single discovery iteration.

        Args:
            prompt: User prompt for this iteration
            reset_session: If True, clear session history before running

        Returns:
            IterationResult with success flag, output text, and token counts
        """
        ...

    async def shutdown(self) -> None:
        """Release all resources held by this executor."""
        ...

    @property
    def total_tokens(self) -> TokenUsage:
        """Cumulative token usage across all iterations."""
        ...
