"""The operator's harness login lives in a home of one container run, outside the
job directory, and goes when the run ends (plans/copied-logins.md)."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import time
from collections.abc import Coroutine, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from docker import errors as docker_errors
from openscientist import web_app
from openscientist.agent.codex_agent import CodexAgent
from openscientist.agent.omp_agent import OmpAgent
from openscientist.job_container.db_login import (
    AGENT_LOGIN_LABEL,
    AgentLogin,
    reap_leftover_agent_logins,
    run_agent_login_reaper,
)
from openscientist.job_container.run_home import (
    RUN_HOMES_DIR,
    remove_legacy_job_homes,
    remove_run_home,
    remove_stale_run_homes,
)
from openscientist.job_container.runner import AGENT_APP_DIR, JobContainerRunner
from openscientist.providers.base import AirgapEgress, AirgapPosture
from tests.admin_row import default_admin_settings_values

_MARKER = "operator-login-marker"


@pytest.fixture(autouse=True)
def _admin_settings_row():
    """The launch reads the row at use, and these tests have no database for it."""
    with patch(
        "openscientist.job_container.runner.current_admin_settings_sync",
        return_value=default_admin_settings_values(),
    ):
        yield


class _Provider:
    def proxied_container_env(self, **_: Any) -> dict[str, str]:
        return {}

    def prelaunch_model_context_env(self) -> dict[str, str]:
        return {}

    def prelaunch_routing_error(self) -> str | None:
        return None

    def airgap_egress(self) -> AirgapPosture:
        return AirgapPosture(AirgapEgress.PROXY)


@dataclass
class _Launches:
    jobs_dir: Path
    client: MagicMock
    runner: JobContainerRunner
    closed: MagicMock
    roles: list[str]

    def launch(self, job_id: str) -> tuple[str, dict[str, Any]]:
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        self.runner.launch(job_id, job_dir)
        return self.roles[-1], self.client.containers.run.call_args.kwargs

    def container_of(self, run: dict[str, Any]) -> MagicMock:
        container = MagicMock()
        container.labels = run["labels"]
        return container


def _settings(*, omp: Path | None = None, codex: Path | None = None, provider_id: str) -> Any:
    return SimpleNamespace(
        container=SimpleNamespace(
            host_project_dir=None,
            container_app_dir="/app",
            agent_network=None,
            agent_memory="8g",
            agent_cpu=2.0,
            agent_platform=None,
            agent_image="openscientist-agent:latest",
            agent_timeout=14400,
        ),
        provider=SimpleNamespace(
            provider_id=provider_id,
            omp_auth_host_path=str(omp) if omp else None,
            codex_auth_host_path=str(codex) if codex else None,
            get_container_env_vars=dict,
        ),
        database=SimpleNamespace(effective_database_url="postgresql+asyncpg://w:p@db:5432/db"),
        phenix=SimpleNamespace(phenix_host_path=None),
        secret_key="master-key",
        airgap=SimpleNamespace(enabled=False),
    )


@pytest.fixture(params=["omp", "codex"])
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[type, Any, str, str | None]:
    """A harness configured with an operator login: its agent class, settings,
    the name of its home inside a job, and the file of the copied login —
    None for every harness, since the web holds the credential (the omp vault
    or the codex auth.json) and signs through the proxy, so the run's home
    stays credential-free."""
    if request.param == "omp":
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "agent.db").write_text(_MARKER)
        (vault / "config.yml").write_text(_MARKER)
        return OmpAgent, _settings(omp=vault, provider_id="vllm"), ".omp-home", None
    auth = tmp_path / "auth.json"
    auth.write_text(_MARKER)
    return CodexAgent, _settings(codex=auth, provider_id="openai"), ".codex", None


@pytest.fixture
def launches(harness: tuple[type, Any, str, str], tmp_path: Path) -> Iterator[_Launches]:
    agent_class, settings, _, _ = harness
    roles: list[str] = []

    def open_login(job_id: str, *, lifetime_seconds: int) -> AgentLogin:
        roles.append(f"agent_{uuid4().hex}")
        return AgentLogin(role=roles[-1], database_url="postgresql+asyncpg://a:b@db:5432/db")

    client = MagicMock()
    with (
        patch("openscientist.job_container.runner.docker.from_env", return_value=client),
        patch("openscientist.job_container.runner.get_settings", return_value=settings),
        patch("openscientist.job_container.runner.get_provider", return_value=_Provider()),
        patch("openscientist.job_container.runner.open_agent_login", side_effect=open_login),
        patch("openscientist.job_container.runner.close_agent_login") as closed,
        patch.object(JobContainerRunner, "_get_network", return_value="bridge"),
        patch("openscientist.agent.factory.agent_class_for_provider_id", return_value=agent_class),
    ):
        yield _Launches(tmp_path / "jobs", client, JobContainerRunner(), closed, roles)


def _home_of(run: dict[str, Any], job_id: str, home_name: str) -> Path:
    """The host directory mounted as the harness's home in ``run``."""
    target = f"{AGENT_APP_DIR}/jobs/{job_id}/{home_name}"
    sources = [src for src, mount in run["volumes"].items() if mount["bind"] == target]
    assert len(sources) == 1, run["volumes"]
    assert run["volumes"][sources[0]]["mode"] == "rw"
    return Path(sources[0])


def test_a_runs_login_is_mounted_from_its_own_home_not_the_job_directory(
    launches: _Launches, harness: tuple[type, Any, str, str | None]
) -> None:
    _, _, home_name, login_file = harness
    _, run = launches.launch("job-1")
    home = _home_of(run, "job-1", home_name)
    if login_file is not None:
        assert (home / login_file).read_text() == _MARKER
    else:
        # The web holds the credential and signs through the proxy, so the
        # container's home carries no login file of its own.
        assert not (home / "agent.db").exists()
        assert not (home / "auth.json").exists()
    assert not home.is_relative_to(launches.jobs_dir / "job-1")
    copied = [p for p in (launches.jobs_dir / "job-1").rglob("*") if p.is_file()]
    assert copied == []


def test_overlapping_runs_have_separate_homes_and_end_separately(
    launches: _Launches, harness: tuple[type, Any, str, str | None]
) -> None:
    _, _, home_name, login_file = harness
    _, first = launches.launch("job-1")
    _, second = launches.launch("job-1")
    first_home, second_home = (
        _home_of(first, "job-1", home_name),
        _home_of(second, "job-1", home_name),
    )
    assert first_home != second_home
    launches.client.containers.list.return_value = [launches.container_of(first)]
    launches.runner.cleanup("job-1")
    assert not first_home.exists()
    if login_file is not None:
        assert (second_home / login_file).read_text() == _MARKER
    else:
        assert not (second_home / "agent.db").exists()
        assert not (second_home / "auth.json").exists()


def test_a_cleanup_by_a_fresh_runner_removes_the_runs_home(
    launches: _Launches, harness: tuple[type, Any, str, str]
) -> None:
    """Startup cleanup after a restart finds the home from the container alone."""
    _, _, home_name, _ = harness
    _, run = launches.launch("job-1")
    home = _home_of(run, "job-1", home_name)
    launches.client.containers.list.return_value = [launches.container_of(run)]
    JobContainerRunner().cleanup("job-1")
    assert not home.exists()


def test_a_launch_that_fails_removes_the_runs_home(
    launches: _Launches, harness: tuple[type, Any, str, str]
) -> None:
    launches.client.containers.run.side_effect = docker_errors.APIError("no such image")
    with pytest.raises(docker_errors.APIError):
        launches.launch("job-1")
    homes = launches.jobs_dir / ".run-homes"
    assert not homes.exists() or not any(homes.iterdir())
    launches.closed.assert_called_once_with(launches.roles[-1])


def test_a_chat_turn_removes_its_home_when_it_ends(
    launches: _Launches, harness: tuple[type, Any, str, str]
) -> None:
    _, _, home_name, _ = harness
    job_dir = launches.jobs_dir / "job-1"
    job_dir.mkdir(parents=True)
    chat = MagicMock()
    chat.wait.return_value = {"StatusCode": 0}

    def started(**kwargs: Any) -> MagicMock:
        chat.labels = kwargs["labels"]
        chat.run_kwargs = kwargs
        return chat

    launches.client.containers.run.side_effect = started
    launches.runner.run_chat_turn("job-1", job_dir, "t1")
    assert not _home_of(chat.run_kwargs, "job-1", home_name).exists()


def _aged(path: Path, age: timedelta) -> None:
    then = time.time() - age.total_seconds()
    os.utime(path, (then, then))


def test_the_reaper_removes_homes_no_container_needs(tmp_path: Path) -> None:
    homes = tmp_path / ".run-homes"
    for role in ("agent_live", "agent_gone", "agent_new"):
        (homes / role).mkdir(parents=True)
        (homes / role / "auth.json").write_text(_MARKER)
    _aged(homes / "agent_live", timedelta(hours=2))
    _aged(homes / "agent_gone", timedelta(hours=2))
    grace = timedelta(minutes=15)
    assert remove_stale_run_homes(tmp_path, None, older_than=grace) == []
    assert remove_stale_run_homes(tmp_path, {"agent_live"}, older_than=grace) == ["agent_gone"]
    assert sorted(p.name for p in homes.iterdir()) == ["agent_live", "agent_new"]


def test_legacy_copies_leave_jobs_that_no_container_runs(tmp_path: Path) -> None:
    for job in ("done", "running"):
        for name in (".codex/auth.json", ".omp-home/agent.db", "final_report.md"):
            (tmp_path / job / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / job / name).write_text(_MARKER)
    assert remove_legacy_job_homes(tmp_path, None) == []
    assert remove_legacy_job_homes(tmp_path, {"running"}) == ["done"]
    assert sorted(p.name for p in (tmp_path / "done").iterdir()) == ["final_report.md"]
    assert (tmp_path / "running" / ".codex" / "auth.json").exists()
    assert (tmp_path / "running" / ".omp-home" / "agent.db").exists()


async def test_the_reaper_drops_stale_logins_and_sweeps_the_homes_docker_left(
    tmp_path: Path,
) -> None:
    """One reaper run asks Docker which logins and jobs still have a container,
    drops the rest from the database, and deletes the homes no container
    needs, with live runs and their job directories left alone."""
    jobs_dir = tmp_path / "jobs"
    homes = jobs_dir / RUN_HOMES_DIR
    for role in ("agent_gone", "agent_live"):
        (homes / role).mkdir(parents=True)
        (homes / role / "auth.json").write_text(_MARKER)
        _aged(homes / role, timedelta(hours=2))
    (homes / "agent_new").mkdir(parents=True)
    (homes / "agent_new" / "auth.json").write_text(_MARKER)
    for job in ("job-done", "job-live"):
        (jobs_dir / job / ".codex").mkdir(parents=True)
        (jobs_dir / job / ".codex" / "auth.json").write_text(_MARKER)
        (jobs_dir / job / "report.md").write_text("report")

    listed = MagicMock()
    listed.live_login_roles.return_value = {"agent_live"}
    listed.live_job_ids.return_value = {"job-live"}
    with (
        patch("openscientist.job_container.runner.JobContainerRunner", return_value=listed),
        patch(
            "openscientist.job_container.db_login.reap_agent_logins",
            new=AsyncMock(return_value=["agent_stale"]),
        ) as reap,
    ):
        dropped = await reap_leftover_agent_logins(jobs_dir)

    assert dropped == ["agent_stale"]
    reap.assert_awaited_once_with({"agent_live"})
    assert not (homes / "agent_gone").exists()
    assert (homes / "agent_live" / "auth.json").read_text() == _MARKER
    assert (homes / "agent_new" / "auth.json").read_text() == _MARKER
    assert not (jobs_dir / "job-done" / ".codex").exists()
    assert (jobs_dir / "job-done" / "report.md").read_text() == "report"
    assert (jobs_dir / "job-live" / ".codex" / "auth.json").read_text() == _MARKER


async def test_the_reaper_leaves_the_filesystem_alone_when_docker_is_unreachable(
    tmp_path: Path,
) -> None:
    """When Docker cannot be asked, no login is dropped and no home or job
    directory is touched, because live means unknown, not absent."""
    jobs_dir = tmp_path / "jobs"
    home = jobs_dir / RUN_HOMES_DIR / "agent_stuck"
    home.mkdir(parents=True)
    (home / "auth.json").write_text(_MARKER)
    _aged(home, timedelta(hours=2))
    (jobs_dir / "job-stuck" / ".codex").mkdir(parents=True)
    (jobs_dir / "job-stuck" / ".codex" / "auth.json").write_text(_MARKER)

    listed = MagicMock()
    listed.live_login_roles.side_effect = RuntimeError("no docker daemon")
    with (
        patch("openscientist.job_container.runner.JobContainerRunner", return_value=listed),
        patch(
            "openscientist.job_container.db_login.reap_agent_logins",
            new=AsyncMock(return_value=[]),
        ) as reap,
    ):
        dropped = await reap_leftover_agent_logins(jobs_dir)

    assert dropped == []
    reap.assert_awaited_once_with(None)
    assert (home / "auth.json").read_text() == _MARKER
    assert (jobs_dir / "job-stuck" / ".codex" / "auth.json").read_text() == _MARKER


async def test_the_login_reaper_reaps_the_jobs_dir_again_and_again(tmp_path: Path) -> None:
    """The web's reaper keeps sweeping the given jobs root for the web's
    lifetime, not only on its first run."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    reaped: list[Path] = []

    async def reap_once(directory: Path) -> list[str]:
        reaped.append(directory)
        return []

    with patch("openscientist.job_container.db_login.reap_leftover_agent_logins", new=reap_once):
        task = asyncio.create_task(run_agent_login_reaper(jobs_dir, interval_seconds=0))
        deadline = time.monotonic() + 5
        while len(reaped) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert len(reaped) >= 3
    assert all(directory is jobs_dir for directory in reaped)


def test_removing_a_home_that_is_already_gone_is_a_silent_noop(tmp_path: Path, caplog) -> None:
    """Teardown and the reaper can both remove a home: a home that an earlier
    removal already took is not an error and surfaces none."""
    home = tmp_path / RUN_HOMES_DIR / "agent_done"
    home.mkdir(parents=True)
    (home / "auth.json").write_text(_MARKER)
    remove_run_home(home)
    with caplog.at_level(logging.ERROR):
        remove_run_home(home)
    assert not home.exists()
    assert caplog.records == []


def test_a_home_that_cannot_be_removed_is_reported_and_kept(tmp_path: Path, caplog) -> None:
    """A removal that fails mid-teardown is reported and left for the
    reaper's next run, with the operator's login still inside it."""
    target = tmp_path / "login-copy"
    target.mkdir()
    (target / "auth.json").write_text(_MARKER)
    home = tmp_path / RUN_HOMES_DIR / "agent_stuck"
    home.parent.mkdir()
    os.symlink(target, home)

    with caplog.at_level(logging.ERROR):
        remove_run_home(home)

    assert home.exists()
    assert (target / "auth.json").read_text() == _MARKER
    assert any("Could not remove run home" in record.message for record in caplog.records)


def test_a_legacy_home_that_cannot_be_removed_is_reported_and_kept(tmp_path: Path, caplog) -> None:
    """A legacy harness home that cannot be removed is reported, keeps the
    operator's login for the reaper's next run, and still names its job as one
    that carried stale copies."""
    target = tmp_path / "login-copy"
    target.mkdir()
    (target / "agent.db").write_text(_MARKER)
    job = tmp_path / "job-done"
    job.mkdir()
    os.symlink(target, job / ".codex")

    with caplog.at_level(logging.ERROR):
        cleaned = remove_legacy_job_homes(tmp_path, {"job-live"})

    assert cleaned == ["job-done"]
    assert (job / ".codex" / "agent.db").read_text() == _MARKER
    assert any("Could not remove" in record.message for record in caplog.records)


def test_live_job_ids_lists_only_the_jobs_docker_still_carries() -> None:
    client = MagicMock()
    listed = [
        MagicMock(labels={"openscientist.job_id": "job-a", AGENT_LOGIN_LABEL: "agent_a"}),
        MagicMock(labels={"openscientist.job_id": "job-b", AGENT_LOGIN_LABEL: "agent_b"}),
        MagicMock(labels={AGENT_LOGIN_LABEL: "agent_unlabeled"}),
    ]
    client.containers.list.return_value = listed
    with patch("openscientist.job_container.runner.docker.from_env", return_value=client):
        runner = JobContainerRunner()
    assert runner.live_job_ids() == {"job-a", "job-b"}
    client.containers.list.assert_called_once_with(all=True, filters={"label": AGENT_LOGIN_LABEL})
    client.containers.list.side_effect = docker_errors.DockerException("no docker daemon")
    assert runner.live_job_ids() is None


async def _noop(*args: object, **kwargs: object) -> None:
    return None


async def test_startup_registers_the_login_reaper_on_the_state_jobs_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The web starts the reaper on the same jobs root the jobs live in, as the
    named background task, so homes get swept for the web's lifetime."""
    jobs_dir = tmp_path / "jobs"
    monkeypatch.setattr(web_app._state, "jobs_dir", jobs_dir)
    monkeypatch.setattr(web_app, "_verify_db_connection_and_rls", _noop)
    monkeypatch.setattr(web_app, "_ensure_default_skill_sources", _noop)
    monkeypatch.setattr("openscientist.admin_settings.seed_admin_settings", _noop)
    monkeypatch.setattr("openscientist.skill_scheduler.start_skill_scheduler", _noop)
    monkeypatch.setattr("openscientist.llm_proxy.start_llm_proxy", _noop)
    monkeypatch.setattr("openscientist.exec_broker.start_exec_broker", _noop)

    reaper_args: list[Path] = []
    reaper_coros: list[Coroutine[None, None, None]] = []

    def fake_reaper(jobs_dir: Path) -> Coroutine[None, None, None]:
        reaper_args.append(jobs_dir)
        coro = _idle()
        reaper_coros.append(coro)
        return coro

    async def _idle() -> None:
        return None

    registered: list[tuple[Coroutine[None, None, None], str]] = []

    def create_background_task(
        coroutine: Coroutine[None, None, None], *, name: str, logger: object | None = None
    ) -> object:
        registered.append((coroutine, name))
        coroutine.close()
        return None

    with (
        patch("openscientist.job_container.db_login.run_agent_login_reaper", new=fake_reaper),
        patch("openscientist.async_tasks.create_background_task", new=create_background_task),
    ):
        await web_app._start_background_tasks(MagicMock())

    assert reaper_args == [jobs_dir]
    # Startup registers other background tasks too; find the reaper by the
    # coroutine it runs.
    matches = [entry for entry in registered if entry[0] is reaper_coros[0]]
    assert len(matches) == 1
    coroutine, name = matches[0]
    assert name == "agent-login-reaper"
    assert inspect.iscoroutine(coroutine)
