"""Oh My Pi (omp) agent backend.

``OmpAgent`` drives omp as a subprocess (``omp -p --mode=json`` per turn, parsing
the JSON-lines stream). Provider-agnostic. Per-job config lives in the job dir omp
treats as its root: a ``--system-prompt`` file, the tools MCP server in
``.omp/mcp.json``, and skills in ``.omp/skills/``. Binary from
``OPENSCIENTIST_OMP_BIN`` or ``omp`` on ``PATH``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import yaml

from openscientist.agent.base import (
    AbstractAgent,
    AgentBackend,
    AgentConfig,
    IterationResult,
    TokenUsage,
    TurnOutcome,
)
from openscientist.providers.base import LLM_PROXY_URL_ENV, Provider
from openscientist.settings import get_settings
from openscientist.transcript import OMP, TranscriptEntry

if TYPE_CHECKING:
    from openscientist.prompts.common import BackendFragments
    from openscientist.settings import Settings

logger = logging.getLogger(__name__)

_MCP_SERVER_NAME = "openscientist-tools"

# Wall-clock bound on one turn; a stuck turn is cut and the loop advances.
_TURN_TIMEOUT_SECONDS = int(os.environ.get("OPENSCIENTIST_OMP_TURN_TIMEOUT", "900"))


def _resolve_omp_bin() -> str:
    """``OPENSCIENTIST_OMP_BIN``, else ``omp`` on ``PATH`` (literal fallback)."""
    override = os.environ.get("OPENSCIENTIST_OMP_BIN")
    if override:
        return override
    return shutil.which("omp") or "omp"


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


class OmpAgent(AbstractAgent[Provider]):
    """Agent that drives the omp harness CLI over ``omp -p --mode=json``."""

    backend = AgentBackend.OMP
    file_write_tool = "write"
    display_name = "Oh My Pi"
    # omp discovers ``.omp/skills/<name>/SKILL.md`` under its cwd (default layout).
    skills_subdir = ".omp/skills"

    def __init__(self, config: AgentConfig, provider: Provider) -> None:
        super().__init__(config, provider)
        self._model_override = config.model_override
        self._session_id: str | None = None

    @classmethod
    def prompt_fragments(cls) -> BackendFragments:
        from openscientist.prompts.omp import OMP_FRAGMENTS

        return OMP_FRAGMENTS

    @classmethod
    def discovery_system_prompt(
        cls, *, use_hypotheses: bool = False, phenix_available: bool = False
    ) -> str:
        # omp takes one system prompt, so (like codex) it gets the full job doc.
        return cls.job_doc(use_hypotheses=use_hypotheses, phenix_available=phenix_available)

    async def prepare_job_workspace(self, *, use_hypotheses: bool = False) -> None:
        await super().prepare_job_workspace(use_hypotheses=use_hypotheses)
        self._write_mcp_config()

    # apply_runtime_environment/chat_*/write_chat_context use the AbstractAgent
    # defaults: omp reads auth from the subprocess env and its model from
    # config.model_override, and folds chat guidance into the system prompt.

    # Host omp store files copied to rebuild the vault in the job workspace
    # (WAL/SHM included so an un-checkpointed db stays readable).
    _OMP_STORE_FILES: ClassVar[tuple[str, ...]] = (
        "agent.db",
        "agent.db-wal",
        "agent.db-shm",
        "config.yml",
    )

    @classmethod
    def provision_host_prelaunch(cls, settings: Settings, job_dir: Path) -> None:
        """Copy the host omp credential vault into the job workspace, agent-writable
        (mounting fails on permissions for the non-root agent), and point
        ``PI_CODING_AGENT_DIR`` at the copy. No-op unless ``omp_auth_host_path``
        is set (the API-key path needs no vault)."""
        src = settings.provider.omp_auth_host_path
        if not src:
            return
        src_path = Path(src).expanduser()
        if not src_path.is_dir():
            logger.warning("omp_auth_host_path %s is not a directory, skipping", src_path)
            return
        dest = job_dir / ".omp-home"
        dest.mkdir(parents=True, exist_ok=True)
        dest.chmod(0o777)
        copied = 0
        for name in cls._OMP_STORE_FILES:
            f = src_path / name
            if f.exists():
                target = dest / name
                shutil.copy2(f, target)
                # Agent opens the SQLite vault read-write, so it must be writable.
                target.chmod(0o666)
                copied += 1
        logger.info("Provisioned omp auth (%d files) into %s", copied, dest)

    def _omp_home(self) -> Path:
        return self._job_dir() / ".omp-home"

    def _job_dir(self) -> Path:
        # Absolute: omp resolves a relative --cwd against its own launch cwd.
        return self._config.job_dir.resolve()

    def _omp_dir(self) -> Path:
        return self._job_dir() / ".omp"

    def _session_dir(self) -> Path:
        return self._omp_dir() / "session"

    def _session_persisted(self, session_id: str) -> bool:
        """True if omp wrote a session store for ``session_id``.

        omp names it ``<timestamp>_<id>.jsonl``, so match on the id alone: the
        timestamp prefix is omp's to change, and a resumed turn appends to the
        file the first turn created rather than adding a new one.
        """
        try:
            return any(session_id in entry.name for entry in self._session_dir().iterdir())
        except OSError:
            return False

    def _model_name(self) -> str | None:
        return self._model_override or self._provider.effective_model_name()

    def _mcp_env(self) -> dict[str, str]:
        """Env table for the tools MCP server, written into ``.omp/mcp.json``.

        Inherited keys are passed as variable *names*, not values. When an omp
        stdio ``env`` value names a set environment variable, omp substitutes
        that variable's value just before launching the server, so the config on
        disk never holds the secret. This matters because the job directory is a
        downloadable artifact and the tools server legitimately needs
        ``DATABASE_URL`` and the exec-broker token.

        The per-job overlay is written literally, because those values are
        computed rather than inherited and a name reference could not resolve
        them. The chat path threads a per-job exec token through that overlay,
        so this alone is not sufficient: ``.omp`` is also excluded from packaged
        artifacts in ``artifact_packager``.
        """
        env = {name: name for name in os.environ}
        env.update(self._job_env_overlay(self._job_dir()))
        return env

    def _write_mcp_config(self) -> None:
        """Write ``.omp/mcp.json`` wiring the ``openscientist-tools`` server."""
        omp_dir = self._omp_dir()
        omp_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "mcpServers": {
                _MCP_SERVER_NAME: {
                    "type": "stdio",
                    "command": sys.executable,
                    "args": ["-m", "openscientist_tools"],
                    "cwd": str(self._job_dir()),
                    "env": self._mcp_env(),
                }
            }
        }
        (omp_dir / "mcp.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    def _write_omp_config(self) -> Path:
        """Write the per-run omp config overlay and return its path.

        ``tools.xdev`` defaults on, which mounts MCP tools as ``xd://`` devices
        driven through ``write`` instead of exposing them as callable tools.
        Disabling it makes the openscientist tools ordinary callable tools, which
        is the shape the shared prompts describe. omp still namespaces them as
        ``mcp__<server>_<tool>``, so ``OMP_FRAGMENTS`` renames the mentions
        through ``mcp_tool_prefix``. The cost is that every enabled tool's schema
        ships on each request, which is why ``_OMP_ENABLED_TOOLS`` is a short
        list.
        """
        omp_dir = self._omp_dir()
        omp_dir.mkdir(parents=True, exist_ok=True)
        path = omp_dir / "omp-config.yml"
        path.write_text(yaml.safe_dump({"tools": {"xdev": False}}), encoding="utf-8")
        return path

    def _write_system_prompt(self) -> Path:
        omp_dir = self._omp_dir()
        omp_dir.mkdir(parents=True, exist_ok=True)
        path = omp_dir / "system_prompt.md"
        path.write_text(self._config.system_prompt or "", encoding="utf-8")
        return path

    def _write_turn_prompt(self, prompt: str) -> Path:
        # Passed as ``@<path>`` so a large prompt never hits the argv limit.
        from openscientist.prompts.common import apply_mcp_tool_prefix

        omp_dir = self._omp_dir()
        omp_dir.mkdir(parents=True, exist_ok=True)
        path = omp_dir / "turn_prompt.md"
        # The orchestrator builds the turn prompt backend-agnostically, so it
        # names MCP tools bare -- right for Claude and codex, but omp exposes
        # them as mcp__openscientist_tools_<name>, and a bare name comes back
        # "Tool <name> not found". prompts.common already rewrites the system
        # prompt; the per-turn instructions were missed, and those are the copy
        # the model acts on, so every execute_code call failed.
        path.write_text(apply_mcp_tool_prefix(prompt, self.prompt_fragments()), encoding="utf-8")
        return path

    def _write_omp_model_catalog(self) -> None:
        """Write the active provider's ``models.yml`` into the omp home, if any.

        Feeds the run's cached window in: resolving it can probe the live server,
        and this runs once per turn.
        """
        catalog = self._provider.omp_model_catalog(
            context_window=self.model_profile.context_window_tokens
        )
        if not catalog:
            return
        home = self._omp_home()
        if not home.exists():
            # Only when we create it: the vault provisioner already made it
            # agent-writable, and chmod on a root-owned dir fails for the agent.
            home.mkdir(parents=True, exist_ok=True)
            home.chmod(0o777)
        path = home / "models.yml"
        path.write_text(yaml.safe_dump(dict(catalog), sort_keys=False), encoding="utf-8")
        logger.info("Wrote omp model catalog to %s", path)

    def _build_subprocess_env(self) -> dict[str, str]:
        """omp process env: inherited env, the provider's container env, then the
        per-job overlay. The container env is overlaid so auth works both in the
        runner (where os.environ is pre-injected) and in the web or chat process
        (where it is not).

        Routing comes last and wins, because ``Provider.harness_env`` is the only
        place that knows how *this* harness reaches the provider. The names differ
        from the Claude Code ones the container env publishes, and letting the
        ambient environment win is how a provider ends up talking to the vendor
        directly with the real credential.
        """
        provider_settings = get_settings().provider
        env = dict(os.environ)
        env.update(provider_settings.get_container_env_vars())
        env.update(self._job_env_overlay(self._job_dir()))

        # Declare a self-hosted model to omp. Its built-in catalog knows hosted
        # APIs only, so without this omp cannot resolve --model and fails with
        # "Model ... not found" before ever reaching the server.
        self._write_omp_model_catalog()

        # Use the provisioned vault (e.g. ChatGPT subscription) as omp's home.
        omp_home = self._omp_home()
        if omp_home.is_dir():
            env["PI_CODING_AGENT_DIR"] = str(omp_home)

        # Provider routing, applied last so it beats any ambient value.
        env.update(self._provider.harness_env(proxy=env.get(LLM_PROXY_URL_ENV)))
        return env

    #: omp built-in tools the discovery loop may use. ``--tools`` is an enable
    #: list over the built-ins, so everything absent here is off, notably omp's
    #: own code execution. It does not reach the MCP tools, which omp validates
    #: separately and always exposes. Analysis MUST go through the
    #: ``execute_code`` MCP tool: it runs in the sandboxed executor container and
    #: captures figures into the report, whereas omp's ``eval`` runs inside the
    #: agent container and only renders figures inline, so its plots never reach
    #: the job artifacts. ``write`` is required because it is this class's
    #: ``file_write_tool``.
    _OMP_ENABLED_TOOLS: ClassVar[tuple[str, ...]] = (
        "read",
        "write",
        "edit",
        "grep",
        "glob",
        "todo",
    )

    def _build_args(
        self, system_prompt_path: Path, prompt_path: Path, config_path: Path
    ) -> list[str]:
        job_dir = self._job_dir()
        args = [
            _resolve_omp_bin(),
            "-p",
            "--mode=json",
            "--no-title",
            "--no-lsp",
            "--no-pty",
            "--auto-approve",
            f"--config={config_path}",
            f"--tools={','.join(self._OMP_ENABLED_TOOLS)}",
            f"--cwd={job_dir}",
            f"--session-dir={self._session_dir()}",
            f"--system-prompt={system_prompt_path}",
        ]
        model = self._model_name()
        if model:
            args.append(f"--model={model}")
        if self._session_id is not None:
            args.append(f"--resume={self._session_id}")
        args.append(f"@{prompt_path}")
        return args

    @staticmethod
    def _usage_from_message(message: dict[str, Any]) -> TokenUsage:
        # omp usage buckets are additive and non-overlapping, so map straight.
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return TokenUsage()
        return TokenUsage(
            input_tokens=_as_int(usage.get("input")),
            output_tokens=_as_int(usage.get("output")),
            cache_read_tokens=_as_int(usage.get("cacheRead")),
            cache_write_tokens=_as_int(usage.get("cacheWrite")),
            reasoning_tokens=_as_int(usage.get("reasoning")),
        )

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        parts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "".join(parts)

    @staticmethod
    def _count_tool_calls(messages: list[dict[str, Any]]) -> int:
        count = 0
        for message in messages:
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            if isinstance(content, list):
                count += sum(
                    1
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "toolCall"
                )
        return count

    @staticmethod
    async def _pump(stream: asyncio.StreamReader | None, sink: bytearray) -> None:
        """Accumulate a pipe into ``sink`` so a timeout still leaves what arrived."""
        if stream is None:
            return
        while chunk := await stream.read(65536):
            sink.extend(chunk)

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
        """Signal omp's whole process group. False when it could not be done.

        Refuses to signal our own group. The safety of the group kill rests
        entirely on ``start_new_session=True`` at spawn, which makes omp a group
        leader. Were that ever dropped, omp would inherit our group and a SIGKILL
        here would take down the runner and the web app with it, so the
        relationship is checked rather than trusted.
        """
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return False
        if pgid == os.getpgid(0):
            logger.error("omp pid %s shares our process group, signalling it alone", proc.pid)
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)
            return False
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)
        return True

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """Terminate the omp process group, then kill what has not exited.

        omp spawns MCP server children, so signalling the direct child leaves them
        running: a timed-out 3 iteration run otherwise left two omp processes and
        two MCP children competing for the same job directory and database rows.
        """
        for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            if proc.returncode is not None:
                return
            OmpAgent._signal_group(proc, sig)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=grace)
        if proc.returncode is None:
            logger.error("omp process %s survived SIGKILL", proc.pid)

    async def _run_omp(self, prompt: str) -> IterationResult:
        """Spawn omp for one turn, parse its JSON stream, build the result.

        Owns the turn timeout rather than leaving it to a ``wait_for`` around this
        coroutine: cancelling the await would abandon the process instead of
        stopping it, and would discard the output already received.
        """
        system_prompt_path = self._write_system_prompt()
        self._write_mcp_config()
        config_path = self._write_omp_config()
        prompt_path = self._write_turn_prompt(prompt)
        args = self._build_args(system_prompt_path, prompt_path, config_path)

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self._job_dir()),
            env=self._build_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout_buf, stderr_buf = bytearray(), bytearray()
        pump = asyncio.gather(
            self._pump(proc.stdout, stdout_buf), self._pump(proc.stderr, stderr_buf)
        )
        timed_out = False
        try:
            # One deadline over both awaits. Bounding only the pipe reads would
            # leave proc.wait() unbounded, so a process that closes its pipes
            # without exiting would hang the turn forever, which is the failure
            # this method exists to prevent. The pump is shielded so expiry
            # cancels the waiting, not the reader, leaving the bytes that already
            # arrived available for the partial parse below.
            async with asyncio.timeout(_TURN_TIMEOUT_SECONDS):
                await asyncio.shield(pump)
                await proc.wait()
        except TimeoutError:
            timed_out = True
            logger.warning("omp turn exceeded %ds, cutting the turn", _TURN_TIMEOUT_SECONDS)
            await self._kill_tree(proc)
        except asyncio.CancelledError:
            # Stopping a job cancels this turn, and abandoning it here would leak
            # the tree exactly as the timeout used to. Signalled synchronously
            # because awaiting a graceful stop inside a cancelled task is not
            # dependable, so there is no reliable window for SIGTERM.
            self._signal_group(proc, signal.SIGKILL)
            raise
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

        stdout_bytes, stderr_bytes = bytes(stdout_buf), bytes(stderr_buf)
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")

        messages: list[dict[str, Any]] = []
        usage = TokenUsage()
        final_output = ""
        stream_error = ""
        for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "session":
                sid = event.get("id")
                if isinstance(sid, str):
                    self._session_id = sid
            elif etype == "message_end":
                message = event.get("message")
                if isinstance(message, dict):
                    messages.append(message)
                    if message.get("role") == "assistant":
                        usage += self._usage_from_message(message)
                        text = self._message_text(message)
                        if text:
                            final_output = text
                        # A model-level failure is reported inside the message,
                        # not as an "error" event, and omp still exits 0. Without
                        # this a turn that never reached the server -- wrong port,
                        # server down, model rejected -- parses as a clean turn
                        # that simply had nothing to say.
                        if message.get("stopReason") == "error":
                            detail = message.get("errorMessage")
                            if isinstance(detail, str) and detail:
                                stream_error = detail
            elif etype == "error":
                msg = event.get("message")
                if isinstance(msg, str):
                    stream_error = msg

        # Usage is accounted before any early return: the model consumed those
        # tokens whether the turn completed, failed, or was cut short, and the
        # partial stream is what makes a timed-out turn's usage recoverable.
        self._token_usage += usage

        # omp announces the session id in its first event but only writes the
        # session file once the turn has something to save. An id with no file
        # behind it makes every later turn die on --resume with
        # 'Session "<id>" not found', and since nothing clears it the same dead id
        # is resent forever -- one empty turn then fails every turn after it.
        # Drop it here so the next turn opens a fresh session instead.
        if self._session_id is not None and not self._session_persisted(self._session_id):
            logger.warning(
                "omp did not persist session %s; the next turn starts fresh", self._session_id
            )
            self._session_id = None

        if timed_out:
            # Shape unchanged from before: only the usage accounting above is new,
            # which is what the review asked for. Surfacing the partial transcript
            # here would change what a timed-out turn persists, so it is left out.
            return IterationResult(
                outcome=TurnOutcome.TIMED_OUT, output="", tool_calls=0, transcript=[], error=""
            )

        tool_calls = self._count_tool_calls(messages)

        # A turn that produced neither text nor a tool call did no work, so if it
        # also reported trouble it failed -- whether omp said so by exiting
        # nonzero or by an errored message on a zero exit. Keying off ``messages``
        # alone missed both: omp emits a message even for a turn that never
        # reached the model, and the empty result then reads as COMPLETED, which
        # the orchestrator cannot tell apart from "the agent had nothing to do".
        # Work that did land still completes, so a late nonzero exit keeps it.
        if not final_output and not tool_calls and (proc.returncode != 0 or stream_error):
            error = stream_error or stderr_text.strip() or f"omp exited with code {proc.returncode}"
            logger.error("omp turn produced no work (exit %s): %s", proc.returncode, error)
            return IterationResult(
                outcome=TurnOutcome.FAILED, output="", tool_calls=0, transcript=[], error=error
            )

        transcript: list[TranscriptEntry] = OMP.deserialize(messages)
        return IterationResult(
            outcome=TurnOutcome.COMPLETED,
            output=final_output,
            tool_calls=tool_calls,
            transcript=transcript,
            error=stream_error,
        )

    async def run_iteration(self, prompt: str, *, reset_session: bool = False) -> IterationResult:
        # reset_session drops the session id so the next run starts fresh.
        if reset_session:
            self._session_id = None
        try:
            # No wait_for here: _run_omp owns the timeout so it can stop the
            # process tree and keep the output that already arrived.
            return await self._run_omp(prompt)
        except Exception as e:
            logger.error("omp run failed: %s", e, exc_info=True)
            return IterationResult(
                outcome=TurnOutcome.FAILED, output="", tool_calls=0, transcript=[], error=str(e)
            )

    async def shutdown(self) -> None:
        logger.debug("OmpAgent shut down")
