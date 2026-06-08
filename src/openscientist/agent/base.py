"""Generic agent base parameterised over the provider family.

`AbstractAgent[P: Provider]` ties an agent runtime to the provider
family it can drive: `ClaudeCodeAgent(AbstractAgent[ClaudeCompatible])`
and `CodexAgent(AbstractAgent[CodexCompatible])` (added later) cannot be
constructed with a mismatched provider, and mypy rejects the mismatch at
check-time. Nothing inherits this yet.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path

from openscientist.agent.mcp_specs import McpServerSpec
from openscientist.providers.base import Provider
from openscientist.transcript import TranscriptEntry

__all__ = [
    "AbstractAgent",
    "AgentConfig",
    "IterationResult",
    "TokenUsage",
    "TranscriptEntry",
]


@dataclass
class TokenUsage:
    """Normalized token usage across all iterations.

    Each token is counted in exactly one category; the categories are
    additive and non-overlapping. Total token count equals the sum of
    all five fields. This invariant lets cost functions multiply each
    field by its per-category rate and sum, without double-counting.

    Backend SDKs that report hierarchical counts (e.g., OpenAI's
    ``input_tokens`` includes ``cached_input_tokens``) must subtract
    sub-categories from totals at the agent boundary before populating
    this dataclass.
    """

    input_tokens: int = 0
    """Fresh, uncached input tokens."""

    output_tokens: int = 0
    """Visible (non-reasoning) output tokens."""

    cache_write_tokens: int = 0
    """Tokens written to a provider-side prompt cache. Anthropic only."""

    cache_read_tokens: int = 0
    """Tokens served from a provider-side prompt cache."""

    reasoning_tokens: int = 0
    """Internal reasoning tokens (o-series; Anthropic extended thinking)."""

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )

    def __iadd__(self, other: TokenUsage) -> TokenUsage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.reasoning_tokens += other.reasoning_tokens
        return self


@dataclass(frozen=True)
class IterationResult:
    """Result of a single agent iteration."""

    success: bool
    output: str
    tool_calls: int
    transcript: list[TranscriptEntry]
    error: str = ""


@dataclass(frozen=True)
class AgentConfig:
    """Backend-agnostic agent configuration."""

    job_dir: Path
    data_file: Path | None = None
    system_prompt: str | None = None
    use_hypotheses: bool = False
    data_files: tuple[Path, ...] = ()
    mcp_servers: tuple[McpServerSpec, ...] = ()
    # Optional per-run model override. Honored by the Claude path (e.g. the
    # ANTHROPIC_CHAT_MODEL escape hatch for in-page chat). The codex path
    # sources its model from the provider, so this is ignored there.
    model_override: str | None = None


class AbstractAgent[P: Provider](abc.ABC):
    """Agent runtime parameterised over the provider family it accepts."""

    def __init__(self, config: AgentConfig, provider: P) -> None:
        self._config = config
        self._provider = provider
        self._token_usage = TokenUsage()

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def provider(self) -> P:
        return self._provider

    @property
    def total_tokens(self) -> TokenUsage:
        return self._token_usage

    @abc.abstractmethod
    async def run_iteration(
        self, prompt: str, *, reset_session: bool = False
    ) -> IterationResult: ...

    @abc.abstractmethod
    async def shutdown(self) -> None: ...
