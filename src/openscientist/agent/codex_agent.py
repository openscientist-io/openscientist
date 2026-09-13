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
    CommandExecutionOutputDeltaNotification,
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

# Live transcript snapshots are intentionally coalesced. App-server can emit
# output deltas much faster than a JSON transcript can be translated and
# atomically replaced, and doing that work inline would stall event intake.
_TRANSCRIPT_FLUSH_INTERVAL_SECONDS = 0.05

# Cleanup must not turn a bounded turn timeout into an unbounded wait when the
# SDK process or one of its async generators is unhealthy.
_CLEANUP_TIMEOUT_SECONDS = 5.0


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
        self._partial_items: dict[str, ThreadItem] = {}
        self._command_output_deltas: dict[str, list[str]] = {}
        self._anonymous_item_sequence = 0
        self._partial_usage: ThreadTokenUsage | None = None
        self._partial_usage_accounted = False
        self._transcript_dirty = False
        self._transcript_flush_task: asyncio.Task[None] | None = None

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
        codex = self._codex
        self._codex = None
        self._thread = None
        if codex is not None:
            await self._run_bounded_cleanup(codex.close(), "closing Codex client")

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

    def _partial_item_values(self) -> list[ThreadItem]:
        """Return items in first-seen order for transcript/result consumers."""
        return self._materialize_partial_items(
            self._partial_items,
            self._command_output_deltas,
        )

    @staticmethod
    def _materialize_partial_items(
        items_by_id: dict[str, ThreadItem], output_deltas: dict[str, list[str]]
    ) -> list[ThreadItem]:
        """Apply buffered command deltas to an ordered item snapshot."""
        items: list[ThreadItem] = []
        for key, item in items_by_id.items():
            deltas = output_deltas.get(key)
            if not deltas:
                items.append(item)
                continue
            item_payload = item.model_dump(mode="json")
            output = str(item_payload.get("aggregated_output") or "") + "".join(deltas)
            items.append(ThreadItem.model_validate({**item_payload, "aggregated_output": output}))
        return items

    def _save_partial_transcript(
        self,
        items_by_id: dict[str, ThreadItem],
        output_deltas: dict[str, list[str]],
    ) -> None:
        """Translate and save a snapshot from a worker thread."""
        items = self._materialize_partial_items(items_by_id, output_deltas)
        save_transcript(self._live_transcript_path(), self._to_transcript(items))

    async def _transcript_writer(self) -> None:
        """Coalesce event bursts and keep blocking JSON/file work off the loop."""
        try:
            await asyncio.sleep(_TRANSCRIPT_FLUSH_INTERVAL_SECONDS)
            while self._transcript_dirty:
                self._transcript_dirty = False
                item_snapshot = dict(self._partial_items)
                delta_snapshot = {
                    key: list(deltas) for key, deltas in self._command_output_deltas.items()
                }
                try:
                    await asyncio.to_thread(
                        self._save_partial_transcript,
                        item_snapshot,
                        delta_snapshot,
                    )
                except Exception:
                    logger.warning("Failed to persist live Codex transcript", exc_info=True)
                if self._transcript_dirty:
                    await asyncio.sleep(_TRANSCRIPT_FLUSH_INTERVAL_SECONDS)
        finally:
            self._transcript_flush_task = None

    def _schedule_partial_transcript_persist(self) -> None:
        """Mark the snapshot dirty and ensure one background writer exists."""
        self._transcript_dirty = True
        task = self._transcript_flush_task
        if task is None or task.done():
            self._transcript_flush_task = asyncio.create_task(self._transcript_writer())

    async def _flush_partial_transcript(self) -> None:
        """Wait until the newest live snapshot has reached disk."""
        if self._transcript_dirty and self._transcript_flush_task is None:
            self._transcript_flush_task = asyncio.create_task(self._transcript_writer())
        while self._transcript_flush_task is not None:
            await self._transcript_flush_task

    def _upsert_partial_item(self, item: Any) -> None:
        """Retain started items and replace them in O(1) by stable item ID."""
        item_payload = item.model_dump(mode="json")
        item_id = item_payload.get("id")
        if item_id:
            key = f"id:{item_id}"
        else:
            self._anonymous_item_sequence += 1
            key = f"anonymous:{self._anonymous_item_sequence}"
        self._partial_items[key] = item
        if item_payload.get("status") != "inProgress":
            self._command_output_deltas.pop(key, None)
        self._schedule_partial_transcript_persist()

    def _append_command_output(self, payload: CommandExecutionOutputDeltaNotification) -> None:
        """Merge command output deltas into the live started-item snapshot."""
        key = f"id:{payload.item_id}"
        item = self._partial_items.get(key)
        if item is None:
            return
        item_payload = item.model_dump(mode="json")
        if item_payload.get("type") != "commandExecution":
            return
        self._command_output_deltas.setdefault(key, []).append(payload.delta)
        self._schedule_partial_transcript_persist()

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
        if getattr(payload, "turn_id", turn_id) != turn_id:
            return None
        if isinstance(payload, ItemStartedNotification):
            if payload.item is not None:
                self._upsert_partial_item(payload.item)
            return None
        if isinstance(payload, ItemCompletedNotification):
            self._upsert_partial_item(payload.item)
            return None
        if isinstance(payload, CommandExecutionOutputDeltaNotification):
            self._append_command_output(payload)
            return None
        if isinstance(payload, ThreadTokenUsageUpdatedNotification):
            self._partial_usage = payload.token_usage
            return None
        if isinstance(payload, TurnCompletedNotification) and payload.turn.id == turn_id:
            return payload
        return None

    def _completed_turn_result(self, completed: TurnCompletedNotification) -> TurnResult:
        """Convert a terminal notification into the SDK aggregate result."""
        status = getattr(completed.turn.status, "value", str(completed.turn.status))
        if status != "completed":
            error = completed.turn.error
            message = getattr(error, "message", None) if error is not None else None
            raise RuntimeError(message or f"Codex turn ended with status {status}")

        items = self._partial_item_values()

        return TurnResult(
            id=completed.turn.id,
            status=completed.turn.status,
            error=completed.turn.error,
            started_at=completed.turn.started_at,
            completed_at=completed.turn.completed_at,
            duration_ms=completed.turn.duration_ms,
            items=items,
            final_response=self._final_response_from_items(items),
            usage=self._partial_usage,
        )

    async def _run_streaming_turn(self, thread: AsyncThread, prompt: str) -> TurnResult:
        """Run a turn while retaining each item as its event arrives."""
        self._partial_items = {}
        self._command_output_deltas = {}
        self._anonymous_item_sequence = 0
        self._partial_usage = None
        self._partial_usage_accounted = False

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
            self._active_turn = None
            await self._run_bounded_cleanup(stream.aclose(), "closing Codex turn stream")
            await self._flush_partial_transcript()

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
        transcript = self._to_transcript(self._partial_item_values())
        transcript.append(
            TaskNotification(
                task_id="codex-turn",
                status=status,
                summary=summary,
                output_file="",
            )
        )
        return transcript

    async def _persist_terminal_transcript(self, transcript: list[TranscriptEntry]) -> None:
        """Write a terminal snapshot after all coalesced live writes finish."""
        await self._flush_partial_transcript()
        try:
            await asyncio.to_thread(save_transcript, self._live_transcript_path(), transcript)
        except Exception:
            logger.warning("Failed to persist terminal Codex notification", exc_info=True)

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[Any]) -> None:
        """Retrieve a detached cleanup task's result to avoid noisy warnings."""
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    @classmethod
    async def _run_bounded_cleanup(cls, awaitable: Any, action: str) -> bool:
        """Run one best-effort cleanup action without waiting indefinitely."""
        task = asyncio.ensure_future(awaitable)
        done, _ = await asyncio.wait({task}, timeout=_CLEANUP_TIMEOUT_SECONDS)
        if not done:
            task.cancel()
            task.add_done_callback(cls._consume_task_exception)
            logger.warning("Timed out while %s", action)
            return False
        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug("Cancelled while %s", action)
            return False
        except Exception:
            logger.debug("Failed while %s", action, exc_info=True)
            return False
        return True

    async def _interrupt_active_turn(self, reason: str) -> bool:
        """Best-effort interruption; report whether a live turn existed."""
        turn = self._active_turn
        if turn is None:
            return False
        await self._run_bounded_cleanup(turn.interrupt(), f"interrupting {reason} Codex turn")
        return True

    @classmethod
    async def _cancel_turn_task(cls, turn_task: asyncio.Task[Any]) -> None:
        """Cancel a turn consumer and retrieve its terminal exception."""
        if not turn_task.done():
            turn_task.cancel()
        done, _ = await asyncio.wait({turn_task}, timeout=_CLEANUP_TIMEOUT_SECONDS)
        if not done:
            turn_task.add_done_callback(cls._consume_task_exception)
            logger.warning("Timed out while cancelling Codex turn task")
            return
        cls._consume_task_exception(turn_task)

    def _account_partial_usage(self) -> None:
        """Accumulate the latest per-turn usage exactly once on every exit."""
        if self._partial_usage is not None and not self._partial_usage_accounted:
            self._token_usage += self._usage_from_payload(self._partial_usage)
            self._partial_usage_accounted = True

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

            await self._interrupt_active_turn("timed-out")
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
        self._account_partial_usage()
        items = self._partial_item_values()
        tool_calls = self._tool_call_count(items)
        transcript = self._partial_transcript_with_notification(
            status="timed_out",
            summary=(
                f"Codex turn exceeded {_TURN_TIMEOUT_SECONDS}s after "
                f"{tool_calls} recorded tool calls."
            ),
        )
        await self._persist_terminal_transcript(transcript)
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
        self._account_partial_usage()
        transcript = self._partial_transcript_with_notification(
            status="failed",
            summary=f"Codex turn failed: {error}",
        )
        await self._persist_terminal_transcript(transcript)
        await self._close_codex()
        return IterationResult(
            outcome=TurnOutcome.FAILED,
            output="",
            tool_calls=self._tool_call_count(self._partial_item_values()),
            transcript=transcript,
            error=str(error),
        )

    async def run_iteration(self, prompt: str, *, reset_session: bool = False) -> IterationResult:
        """Run one turn on the codex thread and return its result.

        The turn's items are translated to a transcript and per-turn token
        usage is accumulated.
        """
        if self._transcript_flush_task is not None:
            await self._transcript_flush_task
        self._partial_items = {}
        self._command_output_deltas = {}
        self._anonymous_item_sequence = 0
        self._partial_usage = None
        self._partial_usage_accounted = False
        try:
            thread = await self._ensure_thread(reset_session)
            result = await self._execute_turn(thread, prompt)
        except asyncio.CancelledError:
            self._account_partial_usage()
            transcript = self._partial_transcript_with_notification(
                status="cancelled",
                summary="Codex turn was cancelled.",
            )
            await self._persist_terminal_transcript(transcript)
            raise
        except TimeoutError:
            # Runaway turn (e.g. the model looping on an unsupported tool call).
            # Report it honestly as TIMED_OUT (work done before the cut is already
            # persisted via the MCP tools); the orchestrator decides whether to
            # advance or fail, rather than this layer claiming success.
            return await self._timed_out_result()
        except Exception as error:
            return await self._failed_result(error)

        if result.usage is not None:
            self._partial_usage = result.usage
        self._account_partial_usage()

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
