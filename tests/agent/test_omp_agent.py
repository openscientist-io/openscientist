"""Tests for `OmpAgent`.

A fake ``omp`` stub (via ``OPENSCIENTIST_OMP_BIN``) records argv and emits a
canned ``--mode=json`` stream, so no real binary or network is needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from openscientist.agent.base import AgentConfig, TurnOutcome
from openscientist.agent.omp_agent import OmpAgent
from openscientist.exceptions import McpToolsUnavailableError
from openscientist.models import ModelProfile
from openscientist.providers.base import OmpModelCatalog, self_hosted_omp_model_catalog
from openscientist.transcript import AssistantText, Reasoning, ToolCall, ToolResult, UserPrompt
from tests.helpers import StubClaudeProvider

_POSIX_PROCESS_GROUP_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="POSIX process groups are unavailable on Windows"
)


class _Provider(StubClaudeProvider):
    """Claude-family stub with a concrete model name for arg assertions."""

    def claude_model_name(self) -> str:
        return "claude-omp-test"


class _CatalogProvider(_Provider):
    """Declares an omp catalog and counts how often the window is resolved."""

    def __init__(self) -> None:
        super().__init__()
        self.profile_calls = 0

    def model_profile(self) -> ModelProfile:
        self.profile_calls += 1
        return ModelProfile(id="claude-omp-test", context_window_tokens=4242)

    def omp_model_catalog(self, *, context_window: int) -> OmpModelCatalog | None:
        return self_hosted_omp_model_catalog(
            provider_id="stub",
            name="Stub",
            base_url="http://127.0.0.1:9/v1",
            model_id="claude-omp-test",
            context_window=context_window,
            api_key=None,
        )


# Canned stream: user, assistant+toolCall, toolResult, final assistant.
# Annotated: the entries are heterogeneous, so mypy would widen the element type
# to object and reject passing this to _write_stub.
_STREAM: list[dict[str, object]] = [
    {"type": "session", "id": "SID-abc123"},
    {"type": "agent_start"},
    {
        "type": "message_end",
        "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]},
    },
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "model": "claude-omp-test",
            "content": [
                {"type": "thinking", "thinking": "planning", "thinkingSignature": "sig"},
                {
                    "type": "toolCall",
                    "id": "call-1",
                    "name": "bash",
                    "arguments": {"command": "echo hi"},
                },
            ],
            "usage": {"input": 10, "output": 20, "cacheRead": 5, "cacheWrite": 3},
        },
    },
    {
        "type": "message_end",
        "message": {
            "role": "toolResult",
            "toolCallId": "call-1",
            "toolName": "bash",
            "content": [{"type": "text", "text": "hi"}],
            "isError": False,
        },
    },
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "model": "claude-omp-test",
            "content": [{"type": "text", "text": "All done."}],
            "usage": {"input": 8, "output": 4, "cacheRead": 1, "cacheWrite": 0},
        },
    },
    {"type": "agent_end", "isTerminal": True},
]


def _write_stub(path: Path, stream: list[dict[str, object]]) -> None:
    """Write a fake-omp that records argv and emits ``stream``.

    Env knobs: ``OMP_STUB_ARGV_OUT``, ``OMP_STUB_SLEEP``, ``OMP_STUB_EXIT``,
    ``OMP_STUB_EMIT``, ``OMP_STUB_MCP_HANDSHAKE_SKIP`` (spawns that skip the
    handshake marker), ``OMP_STUB_FAIL_AFTER`` (spawn index that starts failing),
    ``OMP_STUB_EMIT_FIRST`` (emit before sleeping, so a timeout still has a
    partial stream to parse), and ``OMP_STUB_CHILD_PID_OUT`` (spawn a long-lived
    grandchild and record its pid, standing in for the MCP server children real
    omp starts).
    """
    payload = json.dumps(stream)
    script = f"""#!/usr/bin/env python3
import os, sys, json, time, subprocess
argv_out = os.environ.get("OMP_STUB_ARGV_OUT")
if argv_out:
    with open(argv_out, "w") as fh:
        fh.write("\\n".join(sys.argv))
child_out = os.environ.get("OMP_STUB_CHILD_PID_OUT")
if child_out:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(child_out, "w") as fh:
        fh.write(str(child.pid))


def spawn_index():
    cwd = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--cwd=")), "")
    if not cwd:
        return 0, ""
    counter = os.path.join(cwd, ".omp_stub_spawns")
    try:
        n = int(open(counter).read())
    except (OSError, ValueError):
        n = 0
    with open(counter, "w") as fh:
        fh.write(str(n + 1))
    return n, cwd


SPAWN, CWD = spawn_index()
fail_after = os.environ.get("OMP_STUB_FAIL_AFTER")
failing = fail_after is not None and SPAWN >= int(fail_after)


def write_mcp_handshake():
    # Imitates the real server touching this on tools/list.
    if not CWD or SPAWN < int(os.environ.get("OMP_STUB_MCP_HANDSHAKE_SKIP", "0")):
        return
    d = os.path.join(CWD, ".omp")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "mcp_handshake"), "w").close()


def write_session():
    # Real omp only writes <timestamp>_<id>.jsonl once the turn has something to
    # save, so a stub that always wrote one could not reproduce the empty turn.
    if os.environ.get("OMP_STUB_WRITE_SESSION", "1") != "1":
        return
    sess_dir = next(
        (a.split("=", 1)[1] for a in sys.argv if a.startswith("--session-dir=")), ""
    )
    sid = next(
        (e.get("id") for e in json.loads({payload!r}) if e.get("type") == "session"), ""
    )
    if not sess_dir or not sid:
        return
    os.makedirs(sess_dir, exist_ok=True)
    with open(os.path.join(sess_dir, "2026-01-01T00-00-00-000Z_" + sid + ".jsonl"), "a") as fh:
        fh.write("session\\n")


def emit():
    if failing or os.environ.get("OMP_STUB_EMIT", "1") != "1":
        return
    write_session()
    for event in json.loads({payload!r}):
        print(json.dumps(event), flush=True)


write_mcp_handshake()
emit_first = os.environ.get("OMP_STUB_EMIT_FIRST") == "1"
if emit_first:
    emit()
sleep = float(os.environ.get("OMP_STUB_SLEEP", "0"))
if sleep:
    time.sleep(sleep)
if not emit_first:
    emit()
sys.exit(1 if failing else int(os.environ.get("OMP_STUB_EXIT", "0")))
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _agent(tmp_path: Path, **cfg_kwargs: object) -> OmpAgent:
    config = AgentConfig(job_dir=tmp_path, **cfg_kwargs)  # type: ignore[arg-type]
    return OmpAgent(config, _Provider())


@pytest.fixture
def stub_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_path = tmp_path / "fake_omp"
    _write_stub(bin_path, _STREAM)
    monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
    monkeypatch.setenv("OMP_STUB_ARGV_OUT", str(tmp_path / "argv.txt"))
    return bin_path


class TestBuildArgs:
    def test_core_flags_present(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        args = agent._build_args(tmp_path / "sp.md", tmp_path / "turn.md", tmp_path / "c.yml")
        assert "-p" in args
        assert "--mode=json" in args
        assert "--auto-approve" in args
        assert f"--cwd={tmp_path.resolve()}" in args
        assert f"--system-prompt={tmp_path / 'sp.md'}" in args
        assert f"--session-dir={tmp_path.resolve() / '.omp' / 'session'}" in args
        assert f"--config={tmp_path / 'c.yml'}" in args
        assert "--model=claude-omp-test" in args
        assert args[-1] == f"@{tmp_path / 'turn.md'}"

    def test_code_execution_tools_are_withheld(self, tmp_path: Path) -> None:
        """Analysis must go through execute_code, which runs in the sandboxed
        executor and captures figures, not omp's in-container code tools."""
        agent = _agent(tmp_path)
        flag = next(
            a for a in agent._build_args(tmp_path, tmp_path, tmp_path) if a.startswith("--tools=")
        )
        enabled = set(flag.removeprefix("--tools=").split(","))
        assert "write" in enabled, "write is how omp reaches MCP tools"
        assert enabled.isdisjoint({"eval", "python", "bash", "notebook"})

    def test_resume_only_when_session_known(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        args = agent._build_args(tmp_path, tmp_path, tmp_path)
        assert not any(a.startswith("--resume=") for a in args)
        agent._session_id = "SID-xyz"
        assert "--resume=SID-xyz" in agent._build_args(tmp_path, tmp_path, tmp_path)

    def test_model_override_wins(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, model_override="opus-override")
        assert "--model=opus-override" in agent._build_args(tmp_path, tmp_path, tmp_path)


class TestTurnPrompt:
    def test_skill_bodies_are_namespaced_too(self, tmp_path: Path) -> None:
        """Skills are a third prompt surface. A skill that names `execute_code`
        bare teaches omp's agent a name that returns "Tool ... not found"."""
        from openscientist.database.models import Skill

        skill = Skill(
            slug="demo",
            category="workflow",
            name="Demo",
            description="d",
            content="Run analysis with `execute_code`.",
        )
        agent = _agent(tmp_path)
        agent._write_skill(tmp_path / "skills", skill)
        body = (tmp_path / "skills" / "workflow--demo" / "SKILL.md").read_text()
        assert "`mcp__openscientist_tools_execute_code`" in body
        assert "`execute_code`" not in body

    def test_the_research_question_is_not_rewritten(self, tmp_path: Path) -> None:
        """The turn prompt carries the scientist's own words, and they mark code
        the way anyone does. A question about a local helper called
        ``execute_code`` must reach the model exactly as it was asked."""
        agent = _agent(tmp_path)
        question = "Why does our `execute_code` helper drop rows, and does execute_code() retry?"
        written = agent._write_turn_prompt(question).read_text()
        assert written == question

    def test_skill_callable_form_is_namespaced(self, tmp_path: Path) -> None:
        """The shipped skills call tools without backticks, for example
        ``search_pubmed("...")`` in the metabolomics skill, so backticked-only
        rewriting still leaves a skill teaching an unresolvable name."""
        from openscientist.database.models import Skill

        skill = Skill(
            slug="demo",
            category="workflow",
            name="Demo",
            description="d",
            content='Search with search_pubmed("ATP depletion mechanism").',
        )
        agent = _agent(tmp_path)
        agent._write_skill(tmp_path / "skills", skill)
        body = (tmp_path / "skills" / "workflow--demo" / "SKILL.md").read_text()
        assert 'mcp__openscientist_tools_search_pubmed("ATP depletion mechanism")' in body


class TestMissingMcpToolsGuard:
    """Keys off the server's handshake marker, not the agent's own tool calls."""

    @pytest.mark.asyncio
    async def test_completes_when_the_server_was_asked_for_its_tools(
        self, tmp_path: Path, stub_bin: Path
    ) -> None:
        result = await _agent(tmp_path, system_prompt="SYS").run_iteration("go")
        assert result.outcome is TurnOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_fails_when_the_server_was_never_asked(
        self, tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMP_STUB_MCP_HANDSHAKE_SKIP", "99")
        with pytest.raises(McpToolsUnavailableError, match="not available"):
            await _agent(tmp_path, system_prompt="SYS").run_iteration("go")

    @pytest.mark.asyncio
    async def test_a_marker_left_by_an_earlier_turn_is_not_trusted(
        self, tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each turn respawns the server, so the marker must describe this run."""
        monkeypatch.setenv("OMP_STUB_MCP_HANDSHAKE_SKIP", "99")
        agent = _agent(tmp_path, system_prompt="SYS")
        marker = agent._mcp_handshake_marker()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        with pytest.raises(McpToolsUnavailableError):
            await agent.run_iteration("go")

    @pytest.mark.asyncio
    async def test_a_retry_that_gets_its_tools_completes(
        self, tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMP_STUB_MCP_HANDSHAKE_SKIP", "1")
        result = await _agent(tmp_path, system_prompt="SYS").run_iteration("go")
        assert result.outcome is TurnOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_a_retry_that_fails_is_not_blamed_on_missing_tools(
        self, tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OMP_STUB_MCP_HANDSHAKE_SKIP", "99")
        monkeypatch.setenv("OMP_STUB_FAIL_AFTER", "1")
        result = await _agent(tmp_path, system_prompt="SYS").run_iteration("go")
        assert result.outcome is TurnOutcome.FAILED


class TestOmpConfigOverlay:
    def test_disables_xdev_so_mcp_tools_are_callable(self, tmp_path: Path) -> None:
        """With xdev on, the MCP tools are xd:// devices driven through write
        rather than the callable tools the shared prompts describe."""
        import yaml

        agent = _agent(tmp_path)
        path = agent._write_omp_config()
        assert path == tmp_path.resolve() / ".omp" / "omp-config.yml"
        assert yaml.safe_load(path.read_text()) == {"tools": {"xdev": False}}


class TestModelCatalog:
    def test_reuses_the_runs_cached_window(self, tmp_path: Path) -> None:
        """Resolving the window can probe the live server and the catalog is
        rewritten every turn, so it must reuse the profile the agent cached
        instead of resolving again per turn."""
        import yaml

        provider = _CatalogProvider()
        agent = OmpAgent(AgentConfig(job_dir=tmp_path), provider)

        agent._write_omp_model_catalog()
        agent._write_omp_model_catalog()

        catalog = yaml.safe_load((tmp_path.resolve() / ".omp-home" / "models.yml").read_text())
        assert catalog["providers"]["stub"]["models"][0]["contextWindow"] == 4242
        assert provider.profile_calls == 1


class TestMcpConfig:
    def test_writes_openscientist_tools_server(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, use_hypotheses=True)
        agent._write_mcp_config()
        cfg = json.loads((tmp_path / ".omp" / "mcp.json").read_text())
        server = cfg["mcpServers"]["openscientist-tools"]
        assert server["type"] == "stdio"
        assert server["args"] == ["-m", "openscientist_tools"]
        assert server["cwd"] == str(tmp_path.resolve())
        assert server["env"]["OPENSCIENTIST_JOB_ID"] == tmp_path.resolve().name
        assert server["env"]["OPENSCIENTIST_USE_HYPOTHESES"] == "1"

    def test_inherited_env_is_referenced_by_name_not_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The job dir is a downloadable artifact, so the config must not hold
        secret values. omp substitutes an env value whose entry names a set
        variable, so the name alone is enough to reach the tools server."""
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:hunter2@db:5432/os")
        monkeypatch.setenv("OPENSCIENTIST_SECRET_KEY", "s3cret-master-key")
        agent = _agent(tmp_path)
        agent._write_mcp_config()
        raw = (tmp_path / ".omp" / "mcp.json").read_text()
        env = json.loads(raw)["mcpServers"]["openscientist-tools"]["env"]

        assert env["DATABASE_URL"] == "DATABASE_URL"
        assert env["OPENSCIENTIST_SECRET_KEY"] == "OPENSCIENTIST_SECRET_KEY"
        assert "hunter2" not in raw
        assert "s3cret-master-key" not in raw


class TestRunIteration:
    @pytest.mark.asyncio
    async def test_success_parses_stream(self, tmp_path: Path, stub_bin: Path) -> None:
        agent = _agent(tmp_path, system_prompt="SYS")
        result = await agent.run_iteration("do it")

        assert result.outcome is TurnOutcome.COMPLETED
        assert result.output == "All done."
        assert result.tool_calls == 1
        # Session id captured for continuity, and passed on the next turn.
        assert agent._session_id == "SID-abc123"
        # Usage summed across the two assistant messages, additive per field.
        usage = agent.total_tokens
        assert usage.input_tokens == 18
        assert usage.output_tokens == 24
        assert usage.cache_read_tokens == 6
        assert usage.cache_write_tokens == 3
        # Transcript translated through the OMP deserializer, in order.
        types = [type(e) for e in result.transcript]
        assert types == [UserPrompt, Reasoning, ToolCall, ToolResult, AssistantText]

    @pytest.mark.asyncio
    async def test_second_turn_resumes_session(self, tmp_path: Path, stub_bin: Path) -> None:
        agent = _agent(tmp_path, system_prompt="SYS")
        await agent.run_iteration("first")
        await agent.run_iteration("second")
        argv = (tmp_path / "argv.txt").read_text().splitlines()
        assert "--resume=SID-abc123" in argv

    @pytest.mark.asyncio
    async def test_reset_session_drops_resume(self, tmp_path: Path, stub_bin: Path) -> None:
        agent = _agent(tmp_path, system_prompt="SYS")
        agent._session_id = "STALE"
        await agent.run_iteration("go", reset_session=True)
        argv = (tmp_path / "argv.txt").read_text().splitlines()
        assert not any(a == "--resume=STALE" for a in argv)

    @pytest.mark.asyncio
    async def test_unpersisted_session_is_not_resumed(
        self, tmp_path: Path, stub_bin: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """omp announces a session id even for a turn it never persists. Keeping
        that id made every later turn die on --resume with 'Session not found',
        and re-sending the same dead id made one empty turn fail the whole job."""
        monkeypatch.setenv("OMP_STUB_WRITE_SESSION", "0")
        agent = _agent(tmp_path, system_prompt="SYS")
        await agent.run_iteration("first")
        assert agent._session_id is None

        monkeypatch.setenv("OMP_STUB_WRITE_SESSION", "1")
        await agent.run_iteration("second")
        argv = (tmp_path / "argv.txt").read_text().splitlines()
        assert not any(a.startswith("--resume=") for a in argv)
        # The recovered turn persisted, so continuity resumes from here.
        assert agent._session_id == "SID-abc123"

    @pytest.mark.asyncio
    async def test_model_error_with_no_work_fails_the_turn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """omp reports a model-level failure inside the assistant message and
        still exits 0, so this parsed as a clean turn that had nothing to say."""
        stream: list[dict[str, object]] = [
            {"type": "session", "id": "SID-err"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "Unable to connect. Is the computer able to access the url?",
                    "usage": {"input": 0, "output": 0},
                },
            },
        ]
        bin_path = tmp_path / "fake_omp"
        _write_stub(bin_path, stream)
        monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
        agent = _agent(tmp_path, system_prompt="SYS")
        result = await agent.run_iteration("go")
        assert result.outcome is TurnOutcome.FAILED
        assert "Unable to connect" in result.error

    @pytest.mark.asyncio
    async def test_nonzero_exit_with_a_parsed_message_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Keying the failure branch off ``messages`` let one parsed event mask a
        nonzero exit, so a crashed turn reported COMPLETED with an empty result."""
        stream: list[dict[str, object]] = [
            {"type": "session", "id": "SID-crash"},
            {
                "type": "message_end",
                "message": {"role": "user", "content": [{"type": "text", "text": "go"}]},
            },
        ]
        bin_path = tmp_path / "fake_omp"
        _write_stub(bin_path, stream)
        monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
        monkeypatch.setenv("OMP_STUB_EXIT", "3")
        agent = _agent(tmp_path, system_prompt="SYS")
        result = await agent.run_iteration("go")
        assert result.outcome is TurnOutcome.FAILED
        assert "3" in result.error

    @pytest.mark.asyncio
    async def test_nonzero_exit_no_output_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bin_path = tmp_path / "fake_omp"
        _write_stub(bin_path, _STREAM)
        monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
        monkeypatch.setenv("OMP_STUB_EMIT", "0")
        monkeypatch.setenv("OMP_STUB_EXIT", "3")
        agent = _agent(tmp_path, system_prompt="SYS")
        result = await agent.run_iteration("go")
        assert result.outcome is TurnOutcome.FAILED
        assert result.error

    @pytest.mark.asyncio
    async def test_turn_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_path = tmp_path / "fake_omp"
        _write_stub(bin_path, _STREAM)
        monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
        monkeypatch.setenv("OMP_STUB_SLEEP", "3")
        monkeypatch.setattr("openscientist.agent.omp_agent._TURN_TIMEOUT_SECONDS", 1)
        agent = _agent(tmp_path, system_prompt="SYS")
        result = await agent.run_iteration("go")
        assert result.outcome is TurnOutcome.TIMED_OUT

    @pytest.mark.asyncio
    @_POSIX_PROCESS_GROUP_ONLY
    async def test_timeout_kills_the_process_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """omp spawns MCP children, so signalling only the direct child leaves
        them running and competing for the same job dir and database rows."""
        bin_path = tmp_path / "fake_omp"
        pid_file = tmp_path / "child.pid"
        _write_stub(bin_path, _STREAM)
        monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
        monkeypatch.setenv("OMP_STUB_SLEEP", "120")
        monkeypatch.setenv("OMP_STUB_CHILD_PID_OUT", str(pid_file))
        monkeypatch.setattr("openscientist.agent.omp_agent._TURN_TIMEOUT_SECONDS", 1)
        agent = _agent(tmp_path, system_prompt="SYS")

        result = await agent.run_iteration("go")
        assert result.outcome is TurnOutcome.TIMED_OUT

        assert pid_file.exists(), "stub never recorded its child pid"
        child_pid = int(pid_file.read_text())
        # The grandchild is reparented to init and reaped asynchronously, so poll
        # rather than assume a fixed settling time. Signal 0 probes liveness
        # without delivering anything.
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except OSError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail(f"grandchild {child_pid} survived the turn timeout")

    @pytest.mark.asyncio
    async def test_timeout_still_records_token_usage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The model consumed those tokens even though the turn was cut short, so
        the partial stream has to be parsed rather than discarded."""
        bin_path = tmp_path / "fake_omp"
        _write_stub(bin_path, _STREAM)
        monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
        monkeypatch.setenv("OMP_STUB_EMIT_FIRST", "1")
        monkeypatch.setenv("OMP_STUB_SLEEP", "120")
        monkeypatch.setattr("openscientist.agent.omp_agent._TURN_TIMEOUT_SECONDS", 1)
        agent = _agent(tmp_path, system_prompt="SYS")

        result = await agent.run_iteration("go")
        assert result.outcome is TurnOutcome.TIMED_OUT
        assert agent._token_usage.input_tokens > 0

    @pytest.mark.asyncio
    async def test_closed_pipes_without_exit_still_time_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A process that closes its pipes but does not exit must still be cut.
        Bounding only the pipe reads leaves the reap unbounded and hangs forever."""
        bin_path = tmp_path / "fake_omp"
        bin_path.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time\n"
            "os.close(1)\n"
            "os.close(2)\n"
            "time.sleep(120)\n"
        )
        bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
        monkeypatch.setattr("openscientist.agent.omp_agent._TURN_TIMEOUT_SECONDS", 1)
        agent = _agent(tmp_path, system_prompt="SYS")

        result = await asyncio.wait_for(agent.run_iteration("go"), timeout=30)
        assert result.outcome is TurnOutcome.TIMED_OUT

    @pytest.mark.asyncio
    @_POSIX_PROCESS_GROUP_ONLY
    async def test_cancellation_kills_the_process_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stopping a job cancels the turn, which must not leak the tree either.
        The timeout path is not the only way a turn ends early."""
        bin_path = tmp_path / "fake_omp"
        pid_file = tmp_path / "child.pid"
        _write_stub(bin_path, _STREAM)
        monkeypatch.setenv("OPENSCIENTIST_OMP_BIN", str(bin_path))
        monkeypatch.setenv("OMP_STUB_SLEEP", "120")
        monkeypatch.setenv("OMP_STUB_CHILD_PID_OUT", str(pid_file))
        agent = _agent(tmp_path, system_prompt="SYS")

        task = asyncio.create_task(agent.run_iteration("go"))
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        child_pid = int(pid_file.read_text())
        for _ in range(100):
            try:
                os.kill(child_pid, 0)
            except OSError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail(f"grandchild {child_pid} survived cancellation")

    @pytest.mark.asyncio
    @_POSIX_PROCESS_GROUP_ONLY
    async def test_refuses_to_signal_its_own_process_group(self) -> None:
        """The group kill is only safe because omp is spawned as a group leader.
        Signal 0 is used so this stays harmless even if the guard were broken,
        which would otherwise deliver SIGKILL to the test runner itself."""
        proc = await asyncio.create_subprocess_exec(
            "sleep", "30", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            assert OmpAgent._signal_group(proc, 0) is False
        finally:
            proc.kill()
            await proc.wait()

    @pytest.mark.asyncio
    @_POSIX_PROCESS_GROUP_ONLY
    async def test_signals_the_group_when_session_leader(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            "sleep",
            "30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert OmpAgent._signal_group(proc, 0) is True
        finally:
            proc.kill()
            await proc.wait()


class TestAuthProvisioning:
    """OMP_AUTH_HOST_PATH provisioning and PI_CODING_AGENT_DIR wiring."""

    def test_provisions_store_and_points_agent_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.settings import get_settings

        src = tmp_path / "omp-store"
        src.mkdir()
        (src / "agent.db").write_text("db", encoding="utf-8")
        (src / "agent.db-wal").write_text("wal", encoding="utf-8")
        (src / "config.yml").write_text("cfg", encoding="utf-8")

        monkeypatch.setenv("OMP_AUTH_HOST_PATH", str(src))
        get_settings.cache_clear()
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        try:
            OmpAgent.provision_host_prelaunch(get_settings(), job_dir)
        finally:
            get_settings.cache_clear()

        home = job_dir / ".omp-home"
        assert (home / "agent.db").read_text() == "db"
        assert (home / "config.yml").read_text() == "cfg"

        agent = _agent(job_dir)
        assert agent._build_subprocess_env()["PI_CODING_AGENT_DIR"] == str(home)

    def test_no_provisioning_leaves_agent_dir_unset(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path)
        assert "PI_CODING_AGENT_DIR" not in agent._build_subprocess_env()


class TestRoutingPrecedence:
    def test_provider_routing_beats_ambient_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """harness_env is the only place that knows how omp reaches the provider,
        so an inherited value of the same name must not win. Deferring to the
        ambient env is how a job ends up at the vendor with a real credential."""

        class _Routed(_Provider):
            def harness_env(self, *, proxy: str | None) -> dict[str, str]:
                return {"OPENAI_API_KEY": "routed", "OPENAI_BASE_URL": "http://local:1234"}

        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-ambient-key")
        agent = OmpAgent(AgentConfig(job_dir=tmp_path), _Routed())
        env = agent._build_subprocess_env()
        assert env["OPENAI_API_KEY"] == "routed"
        assert env["OPENAI_BASE_URL"] == "http://local:1234"
