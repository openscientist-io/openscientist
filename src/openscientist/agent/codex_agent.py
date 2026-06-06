"""Codex agent backend.

``CodexAgent`` drives the Codex CLI via ``openai-codex-sdk``. The SDK
spawns ``codex exec`` as a one-shot subprocess per turn; it exposes no
programmatic MCP/config parameter, so per-job configuration (the active
``model_provider``, its ``[model_providers.<id>]`` table, and the
``openscientist-tools`` MCP server) is written to
``$CODEX_HOME/config.toml`` and the child is pointed at it via the
``CODEX_HOME`` environment variable. The system prompt is delivered as
an ``AGENTS.md`` in the working directory (codex's project-doc
mechanism, symmetric to how ``ClaudeCodeAgent`` writes ``CLAUDE.md``).

Each turn's ``ThreadItem`` objects are translated to transcript entries
by the shared ``CODEX`` deserializer (see ``_to_transcript``).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from openai_codex_sdk import Codex, Thread, ThreadItem, ThreadOptions, Usage
from openai_codex_sdk import parsing as _codex_parsing
from openai_codex_sdk.types import FileChangeItem as _SdkFileChangeItem

from openscientist.agent.base import (
    AbstractAgent,
    AgentConfig,
    IterationResult,
    TokenUsage,
    TranscriptEntry,
)
from openscientist.providers.base import CodexCompatible
from openscientist.transcript import CODEX

logger = logging.getLogger(__name__)

_MCP_SERVER_NAME = "openscientist-tools"


class _LenientFileChangeItem(_SdkFileChangeItem):
    """``file_change`` item that tolerates an in-progress status.

    The pinned codex binary streams ``file_change`` items with
    ``status="in_progress"``, but openai-codex-sdk 0.1.11 (the latest
    release) types ``FileChangeItem.status`` as ``Literal["completed",
    "failed"]`` only, so its strict parsing raises and fails the whole turn
    the moment codex writes a file. The status string is informational for
    our transcript, so we widen it to whatever codex emits and keep the run
    alive.
    """

    status: str  # type: ignore[assignment]


# Patch the SDK's event-to-model map so the lenient model is used during parsing.
_codex_parsing._ITEM_MODELS["file_change"] = _LenientFileChangeItem


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


class CodexAgent(AbstractAgent[CodexCompatible]):
    """Agent that drives the Codex CLI (``openai-codex-sdk``)."""

    def __init__(self, config: AgentConfig, provider: CodexCompatible) -> None:
        super().__init__(config, provider)
        self._thread: Thread | None = None

    def _codex_home(self) -> Path:
        return self._config.job_dir / ".codex"

    def _mcp_env(self) -> dict[str, str]:
        """Full environment for the tools MCP server, written into the codex
        config.toml ``[mcp_servers.<name>.env]`` table.

        Unlike a normal subprocess, codex does NOT pass its own process
        environment to MCP server children. It passes only this table. So we
        forward the whole parent environment (PATH, DATABASE_URL,
        OPENSCIENTIST_SECRET_KEY, provider creds, executor image, ...) that the
        tools need, then overlay the per-job ``OPENSCIENTIST_*`` values.

        Subclasses that need to seed from something other than ``os.environ``
        (the air-gap subclass replaces it with an allowlisted view) override
        this method; the per-job overlay lives in :meth:`_overlay_job_env` so
        the override can reuse it without duplicating the overlay logic.
        """
        return self._overlay_job_env(dict(os.environ))

    def _overlay_job_env(self, base: dict[str, str]) -> dict[str, str]:
        """Overlay the per-job ``OPENSCIENTIST_*`` values on ``base``.

        Mutates and returns ``base``. Extracted so subclasses can compose
        their own base seed (e.g. an allowlisted env) with the same per-job
        overlay :meth:`_mcp_env` uses.
        """
        config = self._config
        base.update(
            {
                "OPENSCIENTIST_JOB_ID": config.job_dir.name,
                "OPENSCIENTIST_JOB_DIR": str(config.job_dir),
                "OPENSCIENTIST_USE_HYPOTHESES": "1" if config.use_hypotheses else "0",
            }
        )
        if config.data_file is not None:
            base["OPENSCIENTIST_DATA_FILE"] = str(config.data_file)
        if config.data_files:
            base["OPENSCIENTIST_DATA_FILES"] = os.pathsep.join(str(p) for p in config.data_files)
        return base

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
            (self._config.job_dir / "AGENTS.md").write_text(self._config.system_prompt)

    def _ensure_auth(self) -> None:
        """Make the per-job ``CODEX_HOME`` able to authenticate.

        If an API key is available (provider env or ``OPENAI_API_KEY``),
        codex uses it directly. Otherwise copy the codex CLI's stored OAuth
        login (``~/.codex/auth.json``) into the per-job home so ``codex
        exec`` can authenticate via the ChatGPT subscription.
        """
        if self._provider.codex_sdk_env() or os.environ.get("OPENAI_API_KEY"):
            return
        source = Path.home() / ".codex" / "auth.json"
        dest = self._codex_home() / "auth.json"
        if source.exists() and not dest.exists():
            shutil.copy2(source, dest)
            dest.chmod(0o600)
            logger.info("Provisioned codex auth into per-job CODEX_HOME")

    def _make_codex(self) -> Codex:
        """Build a ``Codex`` whose child reads the per-job config home and
        the provider's auth env."""
        env = {
            **os.environ,
            **self._provider.codex_sdk_env(),
            "CODEX_HOME": str(self._codex_home()),
        }
        return Codex({"env": env})

    def _thread_options(self) -> ThreadOptions:
        """Build the ``ThreadOptions`` for a fresh thread.

        Subclasses that need to tweak fields (the air-gap subclass disables
        ``network_access_enabled`` and ``web_search_enabled``) override this
        and ``return super()._thread_options().model_copy(update={...})`` so
        the base defaults stay in one place.
        """
        return ThreadOptions(
            model=self._provider.codex_model_name(),
            working_directory=str(self._config.job_dir),
            # The agent already runs locked down in its own ephemeral
            # container, which is the real security boundary. Codex's
            # own "workspace-write" sandbox additionally gates MCP tool
            # calls and the headless exec auto-cancels them ("user
            # cancelled MCP tool call"), so no tool ever runs. Full
            # access defers sandboxing to the container, as codex
            # recommends for externally sandboxed automation.
            sandbox_mode="danger-full-access",
            # Headless: no human to approve actions, so never ask.
            approval_policy="never",
            # Job dirs are not git repos. Without this, codex exec
            # refuses to run ("not inside a trusted directory").
            skip_git_repo_check=True,
        )

    def _ensure_thread(self, reset_session: bool) -> Thread:
        """Return a started thread, (re)building it when requested."""
        if reset_session:
            self._thread = None
        if self._thread is None:
            self._write_codex_config()
            self._write_agents_md()
            self._ensure_auth()
            codex = self._make_codex()
            self._thread = codex.start_thread(self._thread_options())
            logger.info("Codex thread started")
        return self._thread

    @staticmethod
    def _usage_from_payload(usage: Usage) -> TokenUsage:
        """Normalize a codex ``Usage`` to ``TokenUsage``.

        Codex reports ``input_tokens`` inclusive of ``cached_input_tokens``
        (Responses-API shape), so the fresh-input count is the difference.
        Codex exposes no reasoning-token count, so ``reasoning_tokens`` is
        always 0; there is no prompt-cache write bucket either.
        """
        return TokenUsage(
            input_tokens=usage.input_tokens - usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cached_input_tokens,
            cache_write_tokens=0,
            reasoning_tokens=0,
        )

    @staticmethod
    def _to_transcript(items: list[ThreadItem]) -> list[TranscriptEntry]:
        """Translate the turn's ``ThreadItem`` objects into transcript
        entries by reusing the ``CODEX`` deserializer.

        The SDK hands us parsed items; ``CODEX.deserialize`` consumes the
        raw ``item.completed`` event shape, so each item is dumped back to
        its wire dict and wrapped in an envelope. This delegates every
        mapping (the ``mcp_tool_call`` split, ``file_change`` fan-out,
        unknown-item handling) to the single tested translator.
        """
        events: list[dict[str, Any]] = [
            {"type": "item.completed", "item": item.model_dump(mode="json")} for item in items
        ]
        return CODEX.deserialize(events)

    async def run_iteration(self, prompt: str, *, reset_session: bool = False) -> IterationResult:
        """Run one turn via ``codex exec`` and return its result.

        The turn's items are translated to a transcript and token usage
        from ``Turn.usage`` is accumulated.
        """
        try:
            thread = self._ensure_thread(reset_session)
            turn = await thread.run(prompt)
        except Exception as e:
            logger.error("Codex run failed: %s", e, exc_info=True)
            self._thread = None
            return IterationResult(
                success=False, output="", tool_calls=0, transcript=[], error=str(e)
            )

        if turn.usage is not None:
            self._token_usage += self._usage_from_payload(turn.usage)

        tool_calls = sum(1 for item in turn.items if item.type != "agent_message")
        return IterationResult(
            success=True,
            output=turn.final_response,
            tool_calls=tool_calls,
            transcript=self._to_transcript(turn.items),
            error="",
        )

    async def shutdown(self) -> None:
        """No-op: ``codex exec`` is one-shot per turn, nothing to close."""
        self._thread = None
        logger.debug("CodexAgent shut down")
