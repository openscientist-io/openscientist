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
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from openscientist.agent.mcp_specs import McpServerSpec
from openscientist.agent.skills import render_skill_md
from openscientist.models import ModelProfile
from openscientist.providers.base import Provider
from openscientist.transcript import TranscriptEntry

if TYPE_CHECKING:
    from openscientist.database.models import Skill
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

logger = logging.getLogger(__name__)


class AgentBackend(enum.Enum):
    """The coding-agent runtime (harness) that drives a job.

    The single source of truth for backend identity. Each concrete
    ``AbstractAgent`` owns one of these. The string values are stable and
    match the historical labels persisted and derived elsewhere, so existing
    data and any string comparisons keep working.
    """

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"
    OMP = "omp"


@dataclass
class TokenUsage:
    """Normalized token usage across all iterations.

    Categories are non-overlapping and additive: the five fields sum to the
    total, so cost functions can rate each field without double-counting.
    Backends whose SDK reports hierarchical counts (e.g. OpenAI folds
    ``cached_input_tokens`` into ``input_tokens``) must subtract sub-categories
    before populating this.
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

    The agent reports what happened, the loop owns the policy. ``TIMED_OUT`` is a
    wall-clock cut (work before it is already persisted via tools), so the loop
    may advance rather than fail. Cancellation propagates as an exception, never
    a turn result.
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
        """True only for a normally completed turn. The discovery loop inspects
        ``outcome`` directly to tell a timeout from a failure."""
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
    # Per-invocation env for the tools subprocess. Threaded here, not via global
    # os.environ, so concurrent chats cannot leak one job's exec token.
    tool_server_env: Mapping[str, str] = field(default_factory=dict)


class AbstractAgent[P: Provider](abc.ABC):
    """Agent runtime parameterised over the provider family it accepts.

    Backend-divergent behavior lives in the abstract members below: a subclass
    that omits one is not instantiable (mypy flags it), and ``backend`` is
    enforced in ``__init_subclass__``.
    """

    #: The backend identity this agent implements. Concrete subclasses MUST set
    #: it, and abc cannot enforce a plain ClassVar, so ``__init_subclass__`` does.
    backend: ClassVar[AgentBackend]

    #: The tool this backend uses to create or overwrite a file (``"apply_patch"``
    #: for codex, ``"Write"`` for Claude). Named verbatim in report prompts so the
    #: model knows which tool to call. Enforced like ``backend``.
    file_write_tool: ClassVar[str]

    #: Human-facing harness label for the UI (``"Claude Code"``, ``"Codex"``,
    #: ``"Oh My Pi"``). Enforced like ``backend``.
    display_name: ClassVar[str]

    #: Job-relative directory this backend materialises skills into (e.g.
    #: ``".claude/skills"``). None disables skill materialisation.
    skills_subdir: ClassVar[str | None] = None

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
        if not getattr(cls, "display_name", None) or not isinstance(cls.display_name, str):
            raise TypeError(
                f"{cls.__name__} must set `display_name: ClassVar[str]` "
                "to the backend's human-facing label."
            )

    def __init__(self, config: AgentConfig, provider: P) -> None:
        self._config = config
        self._provider = provider
        self._token_usage = TokenUsage()
        self._model_profile: ModelProfile | None = None

    async def warm_model_profile(self) -> None:
        """Resolve and cache this run's model profile once, in a thread (the
        provider's resolution may do blocking I/O, e.g. the Ollama probe)."""
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
        """Backend-divergent prompt fragments. Every prompt this backend produces
        flows through them, so its prompts cannot diverge."""

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

    async def prepare_job_workspace(self, *, use_hypotheses: bool = False) -> None:
        """Materialise the per-job workspace (enabled skills in this backend's
        layout). Backends needing a job doc or MCP config override and call
        ``super()``."""
        if self.skills_subdir is None:
            return
        from openscientist.database.session import AsyncSessionLocal
        from openscientist.prompts import get_enabled_skills

        try:
            async with AsyncSessionLocal(thread_safe=True) as session:
                skills = await get_enabled_skills(session)
        except Exception as e:
            logger.warning("Failed to load enabled skills: %s", e)
            return
        if not skills:
            logger.info("No enabled skills to write")
            return
        skills_root = self._config.job_dir / self.skills_subdir
        try:
            for skill in skills:
                self._write_skill(skills_root, skill)
            logger.info("Wrote %d skill files to %s", len(skills), skills_root)
        except Exception as e:
            logger.warning("Failed to write skills to %s: %s", skills_root, e)

    def _write_skill(self, skills_root: Path, skill: Skill) -> None:
        """Write one skill as ``<skills_root>/<name>/SKILL.md`` (the Agent
        Skills layout codex and omp discover). Claude overrides for its own."""
        from openscientist.prompts.common import apply_mcp_tool_prefix

        skill_dir = skills_root / f"{skill.category}--{skill.slug}"
        skill_dir.mkdir(parents=True, exist_ok=True)
        # Skill bodies name MCP tools bare like the rest of the prompts, so a
        # backend that namespaces them needs the same rewrite the system and turn
        # prompts get; otherwise a skill teaches a name that will not resolve.
        # No-op wherever the prefix is empty, which is Claude and codex.
        body = apply_mcp_tool_prefix(render_skill_md(skill), self.prompt_fragments())
        (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")

    def _job_env_overlay(self, job_dir: Path) -> dict[str, str]:
        """Per-job ``OPENSCIENTIST_*`` env for the tools subprocess. Each backend
        passes its own ``job_dir`` so it controls path resolution."""
        config = self._config
        env: dict[str, str] = {
            "OPENSCIENTIST_JOB_ID": job_dir.name,
            "OPENSCIENTIST_JOB_DIR": str(job_dir),
            "OPENSCIENTIST_USE_HYPOTHESES": "1" if config.use_hypotheses else "0",
        }
        if config.data_file is not None:
            env["OPENSCIENTIST_DATA_FILE"] = str(config.data_file)
        if config.data_files:
            env["OPENSCIENTIST_DATA_FILES"] = os.pathsep.join(str(p) for p in config.data_files)
        env.update(config.tool_server_env)
        return env

    def apply_runtime_environment(self) -> None:
        """Process env this backend needs before running. Default no-op (Claude
        sets auth/routing flags)."""
        return None

    @classmethod
    def chat_system_prompt(cls, base_system_prompt: str) -> str:
        """The in-page-chat system prompt. Default folds ``chat_doc`` in, correct
        for backends that read everything from the system prompt (e.g. codex).
        Claude overrides to keep the base prompt and deliver guidance via
        ``.claude/CLAUDE.md`` (written by ``write_chat_context``)."""
        return f"{base_system_prompt}\n\n{cls.chat_doc()}"

    def write_chat_context(self) -> None:
        """Materialise on-disk chat context. Default no-op (Claude writes
        ``.claude/CLAUDE.md``)."""
        return None

    @classmethod
    def chat_model_override(cls) -> str | None:
        """Per-run model override for in-page chat. Default: no override."""
        return None

    @classmethod
    def provision_host_prelaunch(cls, settings: Settings, job_dir: Path) -> None:
        """Host-side, pre-container setup, run in the web/orchestrator process
        before the container launches (hence a classmethod). Default no-op."""
        return None
