"""Codex agent backend.

``CodexAgent`` drives the Codex agent via the official ``openai-codex`` SDK.
The SDK launches ``codex app-server`` as a persistent subprocess and speaks
JSON-RPC to it over stdio, so a single thread spans the whole job and turns are
run on it in sequence. The SDK exposes no programmatic MCP/config parameter for
the provider table or the tools MCP server, so per-job configuration (the active
``model_provider``, its ``[model_providers.<id>]`` table, and the
``openscientist-tools`` MCP server) is written to ``$CODEX_HOME/config.toml``
and the child reads it via the ``CODEX_HOME`` environment variable. The system
prompt is delivered as an ``AGENTS.md`` in the working directory (codex's
project-doc mechanism, symmetric to how ``ClaudeCodeAgent`` writes
``CLAUDE.md``).

The official package ships its codex binary only as a musl-tagged wheel
(``openai-codex-cli-bin``), which does not install on glibc hosts, so that
dependency is dropped (see ``pyproject.toml``) and the binary is provisioned
separately and selected via ``CodexConfig.codex_bin`` (see
``_resolve_codex_bin``).

Each turn's items are translated to transcript entries by the shared ``CODEX``
deserializer (see ``_to_transcript``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    Sandbox,
    TurnResult,
)
from openai_codex.generated.v2_all import (
    ItemCompletedNotification,
    ItemStartedNotification,
    ThreadItem,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
)
from openai_codex.models import Notification

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
from openscientist.transcript.io import save_transcript
from openscientist.transcript.variants import TaskNotification

if TYPE_CHECKING:
    from openscientist.prompts.common import BackendFragments
    from openscientist.settings import Settings

logger = logging.getLogger(__name__)

_MCP_SERVER_NAME = "openscientist-tools"

# A positive allowlist prevents new informational SDK item types from silently
# inflating the tool-call count.
_TOOL_ITEM_TYPES = frozenset(
    {
        "commandExecution",
        "mcpToolCall",
        "fileChange",
        "webSearch",
        "imageGeneration",
        "collabAgentToolCall",
    }
)

# Hard wall-clock bound on a single agent turn. A weak model can get stuck
# retrying an unsupported tool call (e.g. apply_patch) and never end the turn,
# which would otherwise run until the job timeout. When exceeded, the turn is
# cut and the loop continues. Tool calls completed before the cut are already
# persisted. Override with OPENSCIENTIST_CODEX_TURN_TIMEOUT (seconds).
_TURN_TIMEOUT_SECONDS = int(os.environ.get("OPENSCIENTIST_CODEX_TURN_TIMEOUT", "900"))


def _resolve_codex_bin() -> str | None:
    """Locate the codex executable for the SDK to launch.

    An explicit ``OPENSCIENTIST_CODEX_BIN`` wins, otherwise fall back to a
    ``codex`` on ``PATH``. Returns None to let ``CodexConfig`` apply its own
    default (which will raise a clear error if no binary is found), since the
    bundled-binary dependency is intentionally not installed.
    """
    override = os.environ.get("OPENSCIENTIST_CODEX_BIN")
    if override:
        return override
    return shutil.which("codex")


def _toml_str(value: str) -> str:
    """Quote a string as a TOML basic string.

    Escapes backslash and quote, plus the control characters that can appear
    in forwarded environment values (newline, carriage return, tab) which
    would otherwise produce invalid TOML.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


class _TokenBreakdown(Protocol):
    """The nested per-turn counts read off the SDK's ``TokenUsageBreakdown``.

    Structural rather than nominal so the mapper stays checkable while tests
    substitute stubs. Typing these four is what keeps a rename or a change of
    nesting upstream from silently mis-pricing a turn.
    """

    @property
    def input_tokens(self) -> int: ...
    @property
    def cached_input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def reasoning_output_tokens(self) -> int: ...


class _TurnUsage(Protocol):
    """The SDK's ``ThreadTokenUsage``, of which only ``last`` is per-turn."""

    @property
    def last(self) -> _TokenBreakdown | None: ...


class CodexAgent(AbstractAgent[CodexCompatible]):
    """Agent that drives the Codex app-server via the official ``openai-codex``."""

    def __init__(self, config: AgentConfig, provider: CodexCompatible) -> None:
        super().__init__(config, provider)
        self._codex: AsyncCodex | None = None
        self._thread: AsyncThread | None = None
        self._active_turn: AsyncTurnHandle | None = None
        self._partial_items: list[ThreadItem] = []
        self._partial_usage: ThreadTokenUsage | None = None

    backend = AgentBackend.CODEX
    file_write_tool = "apply_patch"
    display_name = "Codex"
    # codex discovers ``.agents/skills/<name>/SKILL.md`` under its cwd; the base
    # class writes them there via the default SKILL.md layout.
    skills_subdir = ".agents/skills"

    @classmethod
    def prompt_fragments(cls) -> BackendFragments:
        from openscientist.prompts.codex import CODEX_FRAGMENTS

        return CODEX_FRAGMENTS

    @classmethod
    def discovery_system_prompt(
        cls, *, use_hypotheses: bool = False, phenix_available: bool = False
    ) -> str:
        # Codex reads a single AGENTS.md, so its discovery system prompt is the
        # full per-job doc (CodexAgent writes it to AGENTS.md from this prompt).
        return cls.job_doc(use_hypotheses=use_hypotheses, phenix_available=phenix_available)

    # apply_runtime_environment, chat_system_prompt, write_chat_context, and
    # chat_model_override use the AbstractAgent defaults: codex configures its
    # child via config.toml (no process-env routing), folds the chat guidance
    # into the system prompt, writes no chat file, and has no model override.

    @classmethod
    def provision_host_prelaunch(cls, settings: Settings, job_dir: Path) -> None:
        """Place the codex CLI auth into the per-job CODEX_HOME so the non-root
        agent (uid 1001) can read it.

        Mounting the host auth file directly fails on the uid/permission
        boundary (the host file is mode 600 owned by another user), so we copy
        it in agent-readable. ``job_dir`` is the runner-local path to the job
        directory (the same path ``setup.py`` writes into), not the
        host-translated bind-mount path, so the copy works whether the web
        server runs on the host or in a container. No-op unless
        ``codex_auth_host_path`` is set (the API-key path needs no file).
        """
        src = settings.provider.codex_auth_host_path
        if not src:
            return
        src_path = Path(src).expanduser()
        if not src_path.exists():
            logger.warning("codex_auth_host_path %s does not exist, skipping", src_path)
            return
        codex_home = job_dir / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        # World-writable so the agent can also write config.toml into CODEX_HOME.
        codex_home.chmod(0o777)
        dest = codex_home / "auth.json"
        shutil.copy2(src_path, dest)
        dest.chmod(0o644)
        logger.info("Provisioned codex auth into %s", dest)

    def _job_dir(self) -> Path:
        # Absolute: codex resolves a relative CODEX_HOME/cwd against its own
        # cwd, doubling a relative job dir (chat passes "jobs/<id>", discovery
        # passes an absolute path).
        return self._config.job_dir.resolve()

    def _codex_home(self) -> Path:
        return self._job_dir() / ".codex"

    def _mcp_env(self) -> dict[str, str]:
        """Full environment for the tools MCP server, written into the codex
        config.toml ``[mcp_servers.<name>.env]`` table.

        Unlike a normal subprocess, codex does NOT pass its own process
        environment to MCP server children. It passes only this table. So we
        forward the whole parent environment (PATH, DATABASE_URL,
        OPENSCIENTIST_SECRET_KEY, provider creds, executor image, ...) that the
        tools need, then overlay the per-job ``OPENSCIENTIST_*`` values.
        """
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
        """Make the per-job ``CODEX_HOME`` able to authenticate.

        If an API key is available (provider env or ``OPENAI_API_KEY``),
        codex uses it directly. Otherwise copy the codex CLI's stored OAuth
        login (``~/.codex/auth.json``) into the per-job home so codex can
        authenticate via the ChatGPT subscription.
        """
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
        home and the provider's auth env, and launches our provisioned binary."""
        env = {
            **os.environ,
            **self._provider.codex_sdk_env(),
            "CODEX_HOME": str(self._codex_home()),
        }
        return AsyncCodex(
            CodexConfig(
                codex_bin=_resolve_codex_bin(),
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
        """Return a started thread, (re)building it when requested.

        The app-server client persists across iterations. A reset starts a new
        thread (a fresh conversation) on the same client.
        """
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
            logger.info("Codex thread started")
        return self._thread

    @staticmethod
    def _usage_from_payload(usage: _TurnUsage) -> TokenUsage:
        """Normalize the turn's token usage to ``TokenUsage``.

        The SDK reports per-turn usage as ``usage.last`` (a
        ``TokenUsageBreakdown``) whose counts nest: ``input_tokens`` includes
        ``cached_input_tokens`` and ``output_tokens`` includes
        ``reasoning_output_tokens`` (Responses-API shape). Both sub-counts are
        subtracted here so the buckets stay non-overlapping and still sum to
        the payload's ``total_tokens``. ``usage.total`` is the running thread
        total, which we do not use since ``_token_usage`` accumulates per turn.
        """
        last = usage.last
        if last is None:
            return TokenUsage()
        return TokenUsage(
            input_tokens=last.input_tokens - last.cached_input_tokens,
            output_tokens=max(last.output_tokens - last.reasoning_output_tokens, 0),
            cache_read_tokens=last.cached_input_tokens,
            # The SDK exposes no cache-write count in either lifetime tier.
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            reasoning_tokens=last.reasoning_output_tokens,
        )

    @staticmethod
    def _to_transcript(items: list[Any]) -> list[TranscriptEntry]:
        """Translate the turn's items into transcript entries by reusing the
        ``CODEX`` deserializer.

        The SDK hands us parsed item objects, but ``CODEX.deserialize`` consumes the
        raw ``item.completed`` event shape, so each item is dumped back to its
        wire dict and wrapped in an envelope. This delegates every mapping to
        the single tested translator.
        """
        events: list[dict[str, Any]] = [
            {"type": "item.completed", "item": item.model_dump(mode="json")} for item in items
        ]
        return CODEX.deserialize(events)

    def _live_transcript_path(self) -> Path:
        """Return the atomically updated transcript exposed during a turn."""
        return self._job_dir() / "provenance" / "current_turn_transcript.json"

    def _persist_partial_transcript(self) -> None:
        """Persist completed work without allowing telemetry to fail the turn."""
        try:
            save_transcript(
                self._live_transcript_path(),
                self._to_transcript(self._partial_items),
            )
        except Exception:
            logger.warning("Failed to persist live Codex transcript", exc_info=True)

    def _upsert_partial_item(self, item: Any) -> None:
        """Retain started items and replace them with completed forms by ID."""
        item_id = item.model_dump(mode="json").get("id")
        if item_id:
            for index, existing in enumerate(self._partial_items):
                if existing.model_dump(mode="json").get("id") == item_id:
                    self._partial_items[index] = item
                    break
            else:
                self._partial_items.append(item)
        else:
            self._partial_items.append(item)
        self._persist_partial_transcript()

    @staticmethod
    def _final_response_from_items(items: list[Any]) -> str:
        """Extract the last final, or phase-less, assistant message."""
        fallback = ""
        for item in reversed(items):
            payload = item.model_dump(mode="json")
            if payload.get("type") != "agentMessage":
                continue
            text = str(payload.get("text") or "")
            phase = payload.get("phase")
            if phase == "finalAnswer":
                return text
            if not fallback and phase is None:
                fallback = text
        return fallback

    def _record_turn_event(self, payload: Any, turn_id: str) -> TurnCompletedNotification | None:
        """Apply one notification to the live turn state."""
        if (
            isinstance(payload, ItemStartedNotification)
            and payload.turn_id == turn_id
            and payload.item is not None
        ):
            self._upsert_partial_item(payload.item)
            return None
        if isinstance(payload, ItemCompletedNotification) and payload.turn_id == turn_id:
            self._upsert_partial_item(payload.item)
            return None
        if isinstance(payload, ThreadTokenUsageUpdatedNotification) and payload.turn_id == turn_id:
            self._partial_usage = payload.token_usage
            return None
        if isinstance(payload, TurnCompletedNotification) and payload.turn.id == turn_id:
            return payload
        return None

    def _completed_turn_result(self, completed: TurnCompletedNotification) -> TurnResult:
        """Convert a terminal notification into the SDK aggregate result."""
        status = getattr(completed.turn.status, "value", str(completed.turn.status))
        if status == "failed":
            error = completed.turn.error
            message = getattr(error, "message", None) if error is not None else None
            raise RuntimeError(message or "Codex turn failed")

        return TurnResult(
            id=completed.turn.id,
            status=completed.turn.status,
            error=completed.turn.error,
            started_at=completed.turn.started_at,
            completed_at=completed.turn.completed_at,
            duration_ms=completed.turn.duration_ms,
            items=list(self._partial_items),
            final_response=self._final_response_from_items(self._partial_items),
            usage=self._partial_usage,
        )

    async def _run_streaming_turn(self, thread: AsyncThread, prompt: str) -> TurnResult:
        """Run a turn while retaining each item as its event arrives."""
        self._partial_items = []
        self._partial_usage = None

        turn = await thread.turn(prompt)
        self._active_turn = turn
        completed: TurnCompletedNotification | None = None
        # AsyncTurn.stream is an async generator at runtime, but the SDK exposes
        # the narrower AsyncIterator annotation. The local cast lets us close it
        # deterministically after completion, interruption, or cancellation.
        stream = cast(AsyncGenerator[Notification, None], turn.stream())
        try:
            async for event in stream:
                terminal = self._record_turn_event(event.payload, turn.id)
                if terminal is not None:
                    completed = terminal
        finally:
            await stream.aclose()
            self._active_turn = None

        if completed is None:
            raise RuntimeError("turn completed event not received")
        return self._completed_turn_result(completed)

    @staticmethod
    def _tool_call_count(items: list[Any]) -> int:
        return sum(
            1 for item in items if item.model_dump(mode="json").get("type") in _TOOL_ITEM_TYPES
        )

    def _partial_transcript_with_notification(
        self, *, status: str, summary: str
    ) -> list[TranscriptEntry]:
        transcript = self._to_transcript(self._partial_items)
        transcript.append(
            TaskNotification(
                task_id="codex-turn",
                status=status,
                summary=summary,
                output_file="",
            )
        )
        try:
            save_transcript(self._live_transcript_path(), transcript)
        except Exception:
            logger.warning("Failed to persist terminal Codex notification", exc_info=True)
        return transcript

    async def _interrupt_active_turn(self, reason: str) -> bool:
        """Best-effort interruption; report whether a live turn existed."""
        turn = self._active_turn
        if turn is None:
            return False
        try:
            await turn.interrupt()
        except Exception:
            logger.debug("Interrupting %s Codex turn failed", reason, exc_info=True)
        return True

    @staticmethod
    async def _cancel_turn_task(turn_task: asyncio.Task[Any]) -> None:
        """Cancel a turn consumer and retrieve its terminal exception."""
        if not turn_task.done():
            turn_task.cancel()
        await asyncio.gather(turn_task, return_exceptions=True)

    async def _execute_turn(self, thread: AsyncThread, prompt: str) -> Any:
        """Run one aggregate or streaming turn with bounded cancellation."""
        if isinstance(thread, AsyncThread):
            turn_coro = self._run_streaming_turn(thread, prompt)
        else:
            # Preserve compatibility with SDK-like test/provider adapters
            # that expose only the older aggregate run contract.
            turn_coro = thread.run(prompt)
        turn_task = asyncio.create_task(turn_coro)
        try:
            done, _ = await asyncio.wait({turn_task}, timeout=_TURN_TIMEOUT_SECONDS)
            if done:
                return turn_task.result()

            if await self._interrupt_active_turn("timed-out"):
                await asyncio.wait({turn_task}, timeout=5.0)
            await self._cancel_turn_task(turn_task)
            raise TimeoutError
        except asyncio.CancelledError:
            await self._interrupt_active_turn("cancelled")
            await self._cancel_turn_task(turn_task)
            await self._close_codex()
            raise

    async def _timed_out_result(self) -> IterationResult:
        """Close a timed-out turn while preserving its partial evidence."""
        logger.warning("Codex turn exceeded %ds, cutting the turn", _TURN_TIMEOUT_SECONDS)
        if self._partial_usage is not None:
            self._token_usage += self._usage_from_payload(self._partial_usage)
        tool_calls = self._tool_call_count(self._partial_items)
        transcript = self._partial_transcript_with_notification(
            status="timed_out",
            summary=(
                f"Codex turn exceeded {_TURN_TIMEOUT_SECONDS}s after "
                f"{tool_calls} recorded tool calls."
            ),
        )
        await self._close_codex()
        return IterationResult(
            outcome=TurnOutcome.TIMED_OUT,
            output="",
            tool_calls=tool_calls,
            transcript=transcript,
            error=f"Codex turn exceeded {_TURN_TIMEOUT_SECONDS}s",
        )

    async def _failed_result(self, error: Exception) -> IterationResult:
        """Close a failed turn while preserving its partial evidence."""
        logger.error("Codex run failed: %s", error, exc_info=True)
        transcript = self._partial_transcript_with_notification(
            status="failed",
            summary=f"Codex turn failed: {error}",
        )
        await self._close_codex()
        return IterationResult(
            outcome=TurnOutcome.FAILED,
            output="",
            tool_calls=self._tool_call_count(self._partial_items),
            transcript=transcript,
            error=str(error),
        )

    async def run_iteration(self, prompt: str, *, reset_session: bool = False) -> IterationResult:
        """Run one turn on the codex thread and return its result.

        The turn's items are translated to a transcript and per-turn token
        usage is accumulated.
        """
        self._partial_items = []
        self._partial_usage = None
        try:
            thread = await self._ensure_thread(reset_session)
            result = await self._execute_turn(thread, prompt)
        except TimeoutError:
            # Runaway turn (e.g. the model looping on an unsupported tool call).
            # Report it honestly as TIMED_OUT (work done before the cut is already
            # persisted via the MCP tools); the orchestrator decides whether to
            # advance or fail, rather than this layer claiming success.
            return await self._timed_out_result()
        except Exception as error:
            return await self._failed_result(error)

        if result.usage is not None:
            self._token_usage += self._usage_from_payload(result.usage)

        tool_calls = self._tool_call_count(result.items)
        return IterationResult(
            outcome=TurnOutcome.COMPLETED,
            output=result.final_response or "",
            tool_calls=tool_calls,
            transcript=self._to_transcript(result.items),
            error="",
        )

    async def shutdown(self) -> None:
        """Close the app-server client."""
        await self._close_codex()
        logger.debug("CodexAgent shut down")
