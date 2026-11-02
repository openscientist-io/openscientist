"""Codex agent backend.

The SDK exposes no programmatic configuration, so per-job provider and MCP
settings are written to ``$CODEX_HOME/config.toml`` and the system prompt is
delivered as ``AGENTS.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ItemCompletedNotification,
    MessagePhase,
    ThreadItem,
    ThreadTokenUsageUpdatedNotification,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
)

from openscientist.agent.base import (
    AbstractAgent,
    AgentBackend,
    AgentConfig,
    IterationResult,
    TokenUsage,
    TranscriptEntry,
    TurnOutcome,
)
from openscientist.providers.base import CodexCompatible
from openscientist.transcript import CODEX

if TYPE_CHECKING:
    from openscientist.prompts.common import BackendFragments
    from openscientist.settings import Settings

logger = logging.getLogger(__name__)

_MCP_SERVER_NAME = "openscientist-tools"

# Item types that are messages or reasoning rather than tool actions. Everything
# else (commandExecution, mcpToolCall, fileChange, ...) counts as a tool call.
_NON_TOOL_ITEM_TYPES = frozenset({"userMessage", "agentMessage", "reasoning"})

# Hard wall-clock bound on a single agent turn. A model can get stuck retrying
# a tool call and never end the turn, which would otherwise run until the job
# timeout. When exceeded, the turn is cut and the loop continues. Tool calls
# completed before the cut are already persisted. Override with
# OPENSCIENTIST_CODEX_TURN_TIMEOUT (seconds).
_TURN_TIMEOUT_SECONDS = int(os.environ.get("OPENSCIENTIST_CODEX_TURN_TIMEOUT", "900"))


def _toml_str(value: str) -> str:
    """Quote a string as a TOML basic string, escaping the control characters that
    can appear in forwarded environment values."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


class _TokenBreakdown(Protocol):
    """The nested counts read off the SDK's ``TokenUsageBreakdown``, structural
    rather than nominal so tests can substitute stubs."""

    @property
    def input_tokens(self) -> int: ...
    @property
    def cached_input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def reasoning_output_tokens(self) -> int: ...


def _grown(before: TokenUsage, after: TokenUsage) -> TokenUsage | None:
    """What a running total gained, or None when any count fell, which is how
    codex restarts its total once the context window fills."""
    grown = TokenUsage(
        input_tokens=after.input_tokens - before.input_tokens,
        output_tokens=after.output_tokens - before.output_tokens,
        cache_read_tokens=after.cache_read_tokens - before.cache_read_tokens,
        reasoning_tokens=after.reasoning_tokens - before.reasoning_tokens,
    )
    counts = (
        grown.input_tokens,
        grown.output_tokens,
        grown.cache_read_tokens,
        grown.reasoning_tokens,
    )
    return None if min(counts) < 0 else grown


def _final_response(items: list[ThreadItem]) -> str | None:
    """The turn's answer, chosen as the SDK chooses ``TurnResult.final_response``:
    the last final-answer message, else the last message without a phase."""
    fallback: str | None = None
    for item in reversed(items):
        message = item.root
        if not isinstance(message, AgentMessageThreadItem):
            continue
        if message.phase == MessagePhase.final_answer:
            return message.text
        if message.phase is None and fallback is None:
            fallback = message.text
    return fallback


class CodexAgent(AbstractAgent[CodexCompatible]):
    """Agent that drives the Codex app-server via the official ``openai-codex``."""

    def __init__(self, config: AgentConfig, provider: CodexCompatible) -> None:
        super().__init__(config, provider)
        self._codex: AsyncCodex | None = None
        self._thread: AsyncThread | None = None
        # The thread's running total at its last usage report, which codex
        # sends after every model request of a turn.
        self._thread_total = TokenUsage()

    backend = AgentBackend.CODEX
    file_write_tool = "apply_patch"
    display_name = "Codex"
    # codex discovers ``.agents/skills/<name>/SKILL.md`` under its cwd.
    skills_subdir = ".agents/skills"

    @classmethod
    def prompt_fragments(cls) -> BackendFragments:
        from openscientist.prompts.codex import CODEX_FRAGMENTS

        return CODEX_FRAGMENTS

    @classmethod
    def discovery_system_prompt(
        cls, *, use_hypotheses: bool = False, phenix_available: bool = False
    ) -> str:
        # Codex reads a single AGENTS.md, so its discovery system prompt is the full
        # per-job doc.
        return cls.job_doc(use_hypotheses=use_hypotheses, phenix_available=phenix_available)

    # The other backend hooks use the AbstractAgent defaults.

    RUN_HOME: ClassVar[str | None] = ".codex"

    @classmethod
    def provision_run_home(cls, settings: Settings, home: Path) -> None:
        """Copy the codex CLI auth into the run's CODEX_HOME (host file is mode 600
        owned by another user), readable by the non-root agent (uid 1001). No-op
        unless ``codex_auth_host_path`` is set (the API-key path needs no file)."""
        src = settings.provider.codex_auth_host_path
        if not src:
            return
        src_path = Path(src).expanduser()
        if not src_path.exists():
            logger.warning("codex_auth_host_path %s does not exist, skipping", src_path)
            return
        dest = home / "auth.json"
        shutil.copy2(src_path, dest)
        dest.chmod(0o644)
        logger.info("Provisioned codex auth into %s", dest)

    def _job_dir(self) -> Path:
        # Absolute, because codex resolves a relative CODEX_HOME/cwd against its own cwd,
        # which would double a relative job dir such as "jobs/<id>".
        return self._config.job_dir.resolve()

    def _codex_home(self) -> Path:
        return self._job_dir() / str(self.RUN_HOME)

    def _mcp_env(self) -> dict[str, str]:
        """Full environment for the tools MCP server, written into the config.toml
        env table. Codex passes no process environment to MCP server children,
        only this table, so the whole parent environment is forwarded and the
        per-job ``OPENSCIENTIST_*`` values are overlaid."""
        env = dict(os.environ)
        env.update(self._job_env_overlay(self._job_dir()))
        return env

    def _write_codex_config(self) -> None:
        """Write the per-job ``$CODEX_HOME/config.toml`` selecting the
        provider and wiring the ``openscientist-tools`` MCP server."""
        home = self._codex_home()
        home.mkdir(parents=True, exist_ok=True)

        lines = [
            f"model_provider = {_toml_str(self._provider.codex_model_provider_id())}",
            *self._provider.codex_config_overrides(),
            "",
            f"[mcp_servers.{_MCP_SERVER_NAME}]",
            f"command = {_toml_str(sys.executable)}",
            'args = ["-m", "openscientist_tools"]',
            f"[mcp_servers.{_MCP_SERVER_NAME}.env]",
            *(f"{key} = {_toml_str(value)}" for key, value in self._mcp_env().items()),
        ]
        (home / "config.toml").write_text("\n".join(lines) + "\n")

    def _write_agents_md(self) -> None:
        """Deliver the system prompt as ``AGENTS.md`` in the working dir."""
        if self._config.system_prompt:
            (self._job_dir() / "AGENTS.md").write_text(self._config.system_prompt)

    def _ensure_auth(self) -> None:
        """Make the per-job ``CODEX_HOME`` able to authenticate, falling back to a copy
        of the codex CLI's stored OAuth login (``~/.codex/auth.json``) when no API key
        is available, so codex can use the ChatGPT subscription."""
        if self._provider.codex_sdk_env() or os.environ.get("OPENAI_API_KEY"):
            return
        source = Path.home() / ".codex" / "auth.json"
        dest = self._codex_home() / "auth.json"
        if source.exists() and not dest.exists():
            shutil.copy2(source, dest)
            dest.chmod(0o600)
            logger.info("Provisioned codex auth into per-job CODEX_HOME")

    def _make_codex(self) -> AsyncCodex:
        """Build an ``AsyncCodex`` whose app-server reads the per-job config
        home and the provider's auth env."""
        env = {
            **os.environ,
            **self._provider.codex_sdk_env(),
            "CODEX_HOME": str(self._codex_home()),
        }
        return AsyncCodex(
            CodexConfig(
                env=env,
                cwd=str(self._job_dir()),
            )
        )

    async def _close_codex(self) -> None:
        """Tear down the app-server client and drop the thread."""
        if self._codex is not None:
            try:
                await self._codex.close()
            except Exception:  # best-effort cleanup
                logger.debug("Closing codex client failed", exc_info=True)
        self._codex = None
        self._thread = None

    async def _ensure_thread(self, reset_session: bool) -> AsyncThread:
        """Return a started thread, (re)building it when requested. A reset starts a
        fresh conversation, but the app-server client persists across iterations."""
        if reset_session:
            self._thread = None
        if self._codex is None:
            self._write_codex_config()
            self._write_agents_md()
            self._ensure_auth()
            self._codex = self._make_codex()
        if self._thread is None:
            self._thread = await self._codex.thread_start(
                model=self._provider.codex_model_name(),
                model_provider=self._provider.codex_model_provider_id(),
                # The agent already runs locked down in its own ephemeral
                # container, which is the real security boundary, so codex gets
                # full filesystem/network access and defers sandboxing to the
                # container, as recommended for externally sandboxed automation.
                sandbox=Sandbox.full_access,
                # Headless: no human to approve, so deny_all (codex policy
                # "never") runs tools immediately. auto_review instead waits on a
                # reviewer that times out in a headless run and fails every call.
                approval_mode=ApprovalMode.deny_all,
                cwd=str(self._job_dir()),
            )
            self._thread_total = TokenUsage()
            logger.info("Codex thread started")
        return self._thread

    @staticmethod
    def _usage_from_breakdown(counts: _TokenBreakdown) -> TokenUsage:
        """Normalize the SDK's token counts to ``TokenUsage``, subtracting the nested
        sub-counts (Responses-API shape) so the buckets stay non-overlapping and
        still sum to the breakdown's ``total_tokens``."""
        return TokenUsage(
            input_tokens=counts.input_tokens - counts.cached_input_tokens,
            output_tokens=max(counts.output_tokens - counts.reasoning_output_tokens, 0),
            cache_read_tokens=counts.cached_input_tokens,
            # The SDK exposes no cache-write count in either lifetime tier.
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            reasoning_tokens=counts.reasoning_output_tokens,
        )

    def _count_usage(self, thread_total: _TokenBreakdown) -> None:
        """Add what the thread spent since its last usage report, taking the growth of
        the running ``total`` (a report's ``last`` covers only the latest request,
        and reports repeat on rate-limit updates, which adds nothing)."""
        total = self._usage_from_breakdown(thread_total)
        grown = _grown(self._thread_total, total)
        if grown is not None:
            self._token_usage += grown
        self._thread_total = total

    async def _run_turn(
        self, thread: AsyncThread, prompt: str
    ) -> tuple[list[ThreadItem], str | None]:
        """Start a turn and read its notifications until it completes, counting
        usage as it is reported, so a cut or cancelled turn keeps what it spent."""
        turn = await thread.turn(prompt)
        items: list[ThreadItem] = []
        completed: Turn | None = None
        async for event in turn.stream():
            payload = event.payload
            if isinstance(payload, ThreadTokenUsageUpdatedNotification):
                self._count_usage(payload.token_usage.total)
            elif isinstance(payload, ItemCompletedNotification) and payload.turn_id == turn.id:
                items.append(payload.item)
            elif isinstance(payload, TurnCompletedNotification) and payload.turn.id == turn.id:
                completed = payload.turn
        if completed is None:
            raise RuntimeError("turn completed event not received")
        if completed.status == TurnStatus.failed:
            message = completed.error.message if completed.error is not None else ""
            raise RuntimeError(message or "turn failed")
        return items, _final_response(items)

    @staticmethod
    def _to_transcript(items: list[Any]) -> list[TranscriptEntry]:
        """Translate the turn's items into transcript entries via the ``CODEX``
        deserializer, dumping each parsed item back to its wire dict and wrapping it
        in an ``item.completed`` envelope, because the deserializer consumes that shape."""
        events: list[dict[str, Any]] = [
            {"type": "item.completed", "item": item.model_dump(mode="json")} for item in items
        ]
        return CODEX.deserialize(events)

    async def run_iteration(self, prompt: str, *, reset_session: bool = False) -> IterationResult:
        """Run one turn on the codex thread and return its result."""
        try:
            thread = await self._ensure_thread(reset_session)
            items, answer = await asyncio.wait_for(
                self._run_turn(thread, prompt), timeout=_TURN_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # Runaway turn (e.g. the model looping on an unsupported tool call). Report it
            # as TIMED_OUT. The work done before the cut is already persisted via the
            # MCP tools, and the orchestrator decides whether to advance or fail.
            logger.warning("Codex turn exceeded %ds, cutting the turn", _TURN_TIMEOUT_SECONDS)
            await self._close_codex()
            return IterationResult(
                outcome=TurnOutcome.TIMED_OUT, output="", tool_calls=0, transcript=[], error=""
            )
        except Exception as e:
            logger.error("Codex run failed: %s", e, exc_info=True)
            await self._close_codex()
            return IterationResult(
                outcome=TurnOutcome.FAILED, output="", tool_calls=0, transcript=[], error=str(e)
            )

        tool_calls = sum(
            1
            for item in items
            if item.model_dump(mode="json").get("type") not in _NON_TOOL_ITEM_TYPES
        )
        return IterationResult(
            outcome=TurnOutcome.COMPLETED,
            output=answer or "",
            tool_calls=tool_calls,
            transcript=self._to_transcript(items),
            error="",
        )

    async def shutdown(self) -> None:
        """Close the app-server client."""
        await self._close_codex()
        logger.debug("CodexAgent shut down")
