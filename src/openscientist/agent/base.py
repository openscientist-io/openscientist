"""Generic agent base parameterised over the provider family.

`AbstractAgent[P: Provider]` ties an agent runtime to the provider
family it can drive: `ClaudeCodeAgent(AbstractAgent[ClaudeCompatible])`
and `CodexAgent(AbstractAgent[CodexCompatible])` cannot be
constructed with a mismatched provider, and mypy rejects the mismatch at
check-time. Both concrete agents subclass this.
"""

from __future__ import annotations

import abc
import asyncio
import enum
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from openscientist.agent.mcp_specs import McpServerSpec
from openscientist.models import ModelProfile
from openscientist.providers.base import Provider
from openscientist.transcript import TranscriptEntry

if TYPE_CHECKING:
    from openscientist.prompts.common import BackendFragments
    from openscientist.settings import Settings

__all__ = [
    "AbstractAgent",
    "AgentBackend",
    "AgentConfig",
    "IterationResult",
    "TokenUsage",
    "TranscriptEntry",
]


class AgentBackend(enum.Enum):
    """The agent runtime that drives a provider family.

    The single source of truth for backend identity. Each concrete
    ``AbstractAgent`` owns one of these. The string values are stable and
    match the historical labels persisted and derived elsewhere, so existing
    data and any string comparisons keep working.
    """

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"

    @property
    def display_name(self) -> str:
        """Human-facing label for the UI."""
        return {
            AgentBackend.CLAUDE_CODE: "Claude Code",
            AgentBackend.CODEX: "Codex",
        }[self]


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


class TurnOutcome(enum.Enum):
    """Outcome of one agent turn, for the orchestrator to interpret.

    The agent reports what happened; the loop owns the policy. ``TIMED_OUT`` is a
    wall-clock cut (any work done before it is already persisted via tools), so
    the loop may advance rather than fail. Cancellation is not represented here:
    it propagates as an exception, never as a turn result.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class IterationResult:
    """Result of a single agent iteration."""

    outcome: TurnOutcome
    output: str
    tool_calls: int
    transcript: list[TranscriptEntry]
    error: str = ""

    @property
    def success(self) -> bool:
        """True only for a normally completed turn. Kept so callers that just
        gate on success keep working; the discovery loop inspects ``outcome``
        directly to tell a timeout apart from a failure."""
        return self.outcome is TurnOutcome.COMPLETED


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
    """Agent runtime parameterised over the provider family it accepts.

    Backend-divergent behavior is expressed as members here so that adding a
    new backend is "subclass and implement the interface": the abstract
    members below cannot be skipped (a subclass that omits one is not
    instantiable, and mypy flags it), and ``backend`` is enforced in
    ``__init_subclass__``.
    """

    #: The backend identity this agent implements. Concrete subclasses MUST set
    #: it, and abc cannot enforce a plain ClassVar, so ``__init_subclass__`` does.
    backend: ClassVar[AgentBackend]

    #: The tool this backend uses to create or overwrite a file (``"apply_patch"``
    #: for codex, ``"Write"`` for Claude). Named verbatim in report prompts so the
    #: model knows which tool to call. Enforced like ``backend``.
    file_write_tool: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Only concrete (instantiable) subclasses must declare these. An
        # intermediate abstract subclass may legitimately leave them unset.
        if inspect.isabstract(cls):
            return
        if not isinstance(getattr(cls, "backend", None), AgentBackend):
            raise TypeError(
                f"{cls.__name__} must set `backend: ClassVar[AgentBackend]` "
                "to an AgentBackend member."
            )
        if not getattr(cls, "file_write_tool", None) or not isinstance(cls.file_write_tool, str):
            raise TypeError(
                f"{cls.__name__} must set `file_write_tool: ClassVar[str]` "
                "to the backend's file-writing tool name."
            )

    def __init__(self, config: AgentConfig, provider: P) -> None:
        self._config = config
        self._provider = provider
        self._token_usage = TokenUsage()
        self._model_profile: ModelProfile | None = None

    async def warm_model_profile(self) -> None:
        """Resolve and cache this run's model profile off the event loop.

        The context window does not change within a job, so resolve it once at
        setup. The provider's resolution may do blocking I/O (the Ollama probe),
        so run it in a thread to keep the event loop responsive.
        """
        self._model_profile = await asyncio.to_thread(self._provider.model_profile)

    @property
    def model_profile(self) -> ModelProfile:
        """This run's model profile, resolved once and cached. Resolves
        synchronously as a fallback if accessed before ``warm_model_profile``."""
        if self._model_profile is None:
            self._model_profile = self._provider.model_profile()
        return self._model_profile

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

    # ----- prompt vocabulary (single substitution path) -----

    @classmethod
    @abc.abstractmethod
    def prompt_fragments(cls) -> BackendFragments:
        """The backend-divergent prompt fragments this agent uses.

        Every prompt this backend produces flows through these fragments, so
        the system prompt, job doc, and chat context cannot diverge.
        """

    @classmethod
    def system_prompt(cls) -> str:
        """The concise system prompt for this backend."""
        from openscientist.prompts.common import build_system_prompt

        return build_system_prompt(cls.prompt_fragments())

    @classmethod
    def job_doc(cls, *, use_hypotheses: bool = False, phenix_available: bool = False) -> str:
        """The full per-job instruction doc for this backend."""
        from openscientist.prompts.common import build_job_doc

        return build_job_doc(
            use_hypotheses=use_hypotheses,
            phenix_available=phenix_available,
            frags=cls.prompt_fragments(),
        )

    @classmethod
    def chat_doc(cls) -> str:
        """The in-page-chat guidance for this backend (fragments substituted)."""
        from openscientist.prompts.common import render_chat_context

        return render_chat_context(cls.prompt_fragments())

    @classmethod
    @abc.abstractmethod
    def discovery_system_prompt(
        cls, *, use_hypotheses: bool = False, phenix_available: bool = False
    ) -> str:
        """The system prompt this backend uses for a discovery run.

        Claude returns the concise ``system_prompt`` (its rich doc is written
        into ``.claude/``); codex returns the full ``job_doc`` (delivered via
        ``AGENTS.md``).
        """

    # ----- per-job side effects (run where the agent instance lives) -----

    @abc.abstractmethod
    async def prepare_job_workspace(self, *, use_hypotheses: bool = False) -> None:
        """Materialise per-job files in the backend's layout (e.g. skills).

        Runs in the agent process for the configured ``job_dir``.
        """

    def apply_runtime_environment(self) -> None:
        """Apply any process environment this backend needs before running.

        Default no-op. The Claude backend overrides to set auth/routing flags.
        """
        return None

    @classmethod
    def chat_system_prompt(cls, base_system_prompt: str) -> str:
        """The in-page-chat system prompt for this backend.

        Default folds the fragment-substituted ``chat_doc`` into the prompt,
        which is correct for backends (e.g. codex) that read everything from
        the system prompt. Claude overrides to return the base prompt unchanged
        and deliver the chat guidance via ``.claude/CLAUDE.md`` (written by
        ``write_chat_context``). Pure: the side effects live in
        ``write_chat_context`` so the chat executor can be built once.
        """
        return f"{base_system_prompt}\n\n{cls.chat_doc()}"

    def write_chat_context(self) -> None:
        """Materialise any on-disk in-page-chat context for this backend.

        Default no-op. The Claude backend overrides to write ``.claude/CLAUDE.md``.
        """
        return None

    @classmethod
    def chat_model_override(cls) -> str | None:
        """Per-run model override for in-page chat. Default: no override."""
        return None

    @classmethod
    def provision_host_prelaunch(cls, settings: Settings, job_dir: Path) -> None:
        """Host-side, pre-container setup for this backend.

        Runs in the web/orchestrator process (where no agent instance exists)
        before the agent container is launched, so it is a classmethod keyed
        by the agent class. Default no-op; a backend that needs file-based
        auth provisioning overrides it.
        """
        return None
