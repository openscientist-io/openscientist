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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, CodexConfig, Sandbox

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


class CodexAgent(AbstractAgent[CodexCompatible]):
    """Agent that drives the Codex app-server via the official ``openai-codex``."""

    def __init__(self, config: AgentConfig, provider: CodexCompatible) -> None:
        super().__init__(config, provider)
        self._codex: AsyncCodex | None = None
        self._thread: AsyncThread | None = None

    backend = AgentBackend.CODEX
    file_write_tool = "apply_patch"

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

    async def prepare_job_workspace(self, *, use_hypotheses: bool = False) -> None:
        from openscientist.agent.skills import write_skills_to_codex_dir

        await write_skills_to_codex_dir(self._config.job_dir)

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
        config = self._config
        job_dir = self._job_dir()
        env = dict(os.environ)
        env.update(
            {
                "OPENSCIENTIST_JOB_ID": job_dir.name,
                "OPENSCIENTIST_JOB_DIR": str(job_dir),
                "OPENSCIENTIST_USE_HYPOTHESES": "1" if config.use_hypotheses else "0",
            }
        )
        if config.data_file is not None:
            env["OPENSCIENTIST_DATA_FILE"] = str(config.data_file)
        if config.data_files:
            env["OPENSCIENTIST_DATA_FILES"] = os.pathsep.join(str(p) for p in config.data_files)
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
    def _usage_from_payload(usage: Any) -> TokenUsage:
        """Normalize the turn's token usage to ``TokenUsage``.

        The SDK reports per-turn usage as ``usage.last`` (a
        ``TokenUsageBreakdown``) with ``input_tokens`` inclusive of
        ``cached_input_tokens`` (Responses-API shape), so the fresh-input count
        is the difference. ``usage.total`` is the running thread total, which we
        do not use here since ``_token_usage`` accumulates per turn.
        """
        last = getattr(usage, "last", None)
        if last is None:
            return TokenUsage()
        return TokenUsage(
            input_tokens=last.input_tokens - last.cached_input_tokens,
            output_tokens=last.output_tokens,
            cache_read_tokens=last.cached_input_tokens,
            cache_write_tokens=0,
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

    async def run_iteration(self, prompt: str, *, reset_session: bool = False) -> IterationResult:
        """Run one turn on the codex thread and return its result.

        The turn's items are translated to a transcript and per-turn token
        usage is accumulated.
        """
        try:
            thread = await self._ensure_thread(reset_session)
            result = await asyncio.wait_for(thread.run(prompt), timeout=_TURN_TIMEOUT_SECONDS)
        except TimeoutError:
            # Runaway turn (e.g. the model looping on an unsupported tool call).
            # Report it honestly as TIMED_OUT (work done before the cut is already
            # persisted via the MCP tools); the orchestrator decides whether to
            # advance or fail, rather than this layer claiming success.
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

        if result.usage is not None:
            self._token_usage += self._usage_from_payload(result.usage)

        tool_calls = sum(
            1
            for item in result.items
            if item.model_dump(mode="json").get("type") not in _NON_TOOL_ITEM_TYPES
        )
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
