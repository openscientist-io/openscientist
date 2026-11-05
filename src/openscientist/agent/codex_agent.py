"""Codex agent backend.

The SDK exposes no programmatic configuration, so per-job provider and MCP
settings are written to ``$CODEX_HOME/config.toml`` and the system prompt is
delivered as ``AGENTS.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    ItemCompletedNotification,
    MessagePhase,
    ThreadItem,
    Turn,
    TurnCompletedNotification,
    TurnStatus,
)

from openscientist.agent.base import (
    AbstractAgent,
    AgentBackend,
    AgentConfig,
    IterationResult,
    TranscriptEntry,
    TurnOutcome,
)
from openscientist.providers.base import CodexCompatible
from openscientist.transcript import CODEX

if TYPE_CHECKING:
    from openscientist.prompts.common import BackendFragments

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

    backend = AgentBackend.CODEX
    file_write_tool = "apply_patch"
    display_name = "Codex"
    # codex discovers ``.agents/skills/<name>/SKILL.md`` under its cwd.
    skills_subdir = ".agents/skills"

    @classmethod
    def prompt_fragments(cls) -> BackendFragments:
        from openscientist.prompts.codex import CODEX_FRAGMENTS

        return CODEX_FRAGMENTS

    # The other backend hooks use the AbstractAgent defaults.

    RUN_HOME: ClassVar[str | None] = ".codex"

    def _codex_home(self) -> Path:
        return self._run_home()

    def _mcp_env(self) -> dict[str, str]:
        """Full environment for the tools MCP server, written into the config.toml
        env table. Codex passes no process environment to MCP server children,
        only this table, so the whole parent environment is forwarded and the
        per-job ``OPENSCIENTIST_*`` values are overlaid."""
        return super()._subprocess_env(self._job_dir())

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

    async def _run_turn(
        self, thread: AsyncThread, prompt: str
    ) -> tuple[list[ThreadItem], str | None]:
        """Start a turn and read its notifications until it completes."""
        turn = await thread.turn(prompt)
        items: list[ThreadItem] = []
        completed: Turn | None = None
        async for event in turn.stream():
            payload = event.payload
            if isinstance(payload, ItemCompletedNotification) and payload.turn_id == turn.id:
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
            # Runaway turn (e.g. the model looping on an unsupported tool call).
            logger.warning("Codex turn exceeded %ds, cutting the turn", _TURN_TIMEOUT_SECONDS)
            await self._close_codex()
            return self.timed_out_result()
        except Exception as e:
            logger.error("Codex run failed: %s", e, exc_info=True)
            await self._close_codex()
            return self.failed_result(e)

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
