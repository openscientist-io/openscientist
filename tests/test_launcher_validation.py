"""Pure validation of launcher requests, without a Docker daemon."""

from __future__ import annotations

import base64
import dataclasses
import json
import socket
from pathlib import Path
from typing import Any, cast

import pytest

from openscientist_launcher.api import _airgap_hosts
from openscientist_launcher.config import LauncherConfig
from openscientist_launcher.host_paths import jobs_root_problem
from openscientist_launcher.templates import agent_run_kwargs, executor_run_kwargs
from openscientist_launcher.validation import (
    AGENT_ENV_ALLOWLIST,
    ValidationError,
    validate_agent_request,
    validate_exec_request,
)

JOB_ID = "11111111-2222-3333-4444-555555555555"
LOGIN_ROLE = "agent_" + "a" * 32


def _config(jobs_root: Path, **overrides: object) -> LauncherConfig:
    cfg = LauncherConfig(
        jobs_root=jobs_root,
        agent_image="agent:latest",
        executor_image="executor:latest",
        agent_image_digests=frozenset({"sha256:" + "a" * 64}),
        executor_image_digests=frozenset({"sha256:" + "b" * 64}),
        instance_id="test",
        web_uid=1000,
        web_gid=1000,
        socket_path=jobs_root / "launcher.sock",
        network="bridge",
        agent_platform=None,
        phenix_host_path=None,
        agent_max_memory_mib=8192,
        agent_max_cpu=4.0,
        executor_max_memory_mib=2048,
        executor_max_cpu=1.0,
        max_lifetime_seconds=3600,
        max_egress_entries=8,
    )
    return dataclasses.replace(cfg, **cast("dict[str, Any]", overrides)) if overrides else cfg


def _jobs(tmp_path: Path) -> Path:
    jobs = tmp_path / "jobs"
    (jobs / JOB_ID).mkdir(parents=True)
    return jobs


def _agent_request(**overrides) -> dict[str, object]:
    body: dict[str, object] = {
        "job_id": JOB_ID,
        "mode": "discovery",
        "lifetime_seconds": 600,
        "env": {"JOB_ID": JOB_ID, "DATABASE_URL": "postgresql://w:p@db:5432/os"},
        "db_login": LOGIN_ROLE,
        "egress_allow": ["openscientist:8081", "openscientist:8082"],
        "memory_mib": 4096,
        "cpu": 2.0,
    }
    body.update(overrides)
    return body


class TestAgentRequestValidation:
    def test_valid_request_passes(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        launch = validate_agent_request(_agent_request(), cfg)
        assert launch.job_id == JOB_ID
        assert launch.mode == "discovery"
        assert launch.memory_mib == 4096
        assert launch.cpu == 2.0
        assert launch.airgapped is False
        assert launch.egress_allow == ("openscientist:8081", "openscientist:8082")

    @pytest.mark.parametrize(
        "field",
        [
            "privileged",
            "cap_add",
            "volumes",
            "network",
            "user",
            "image",
            "labels",
            "security_opt",
            "entrypoint",
            "extra_hosts",
            "stop_signal",
        ],
    )
    def test_unknown_top_level_field_refused(self, tmp_path: Path, field: str) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(**{field: "injected"}), cfg)

    def test_malformed_job_id_refused(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(job_id="not-a-uuid"), cfg)

    def test_job_id_without_directory_refused(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        cfg = _config(jobs)
        other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        (jobs / other).mkdir()
        with pytest.raises(ValidationError):
            validate_agent_request(
                _agent_request(job_id="ffffffff-0000-1111-2222-333344445555"), cfg
            )

    @pytest.mark.parametrize("mode", ["", "discoveryy", "REPORT_ONLY", "agent"])
    def test_bad_mode_refused(self, tmp_path: Path, mode: str) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(mode=mode), cfg)

    def test_chat_mode_requires_chat_turn(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(mode="chat"), cfg)
        launch = validate_agent_request(_agent_request(mode="chat", chat_turn="hello"), cfg)
        assert launch.chat_turn == "hello"

    def test_chat_turn_refused_outside_chat_mode(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(chat_turn="hello"), cfg)

    def test_lifetime_above_bound_refused(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(lifetime_seconds=3601), cfg)
        launch = validate_agent_request(_agent_request(lifetime_seconds=3600), cfg)
        assert launch.lifetime_seconds == 3600

    @pytest.mark.parametrize("lifetime", [0, -5, "600", 60.5])
    def test_lifetime_not_a_positive_integer(self, tmp_path: Path, lifetime: object) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(lifetime_seconds=lifetime), cfg)

    def test_env_unknown_key_refused(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(env={"NOT_A_REAL_KEY": "x"}), cfg)

    def test_env_non_string_value_refused(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(env={"JOB_ID": 7}), cfg)

    @pytest.mark.parametrize(
        "key",
        [
            "JOB_ID",
            "DATABASE_URL",
            "OPENSCIENTIST_SECRET_KEY",
            # The backend-agnostic provider identity keys every container env
            # carries; the agent image needs them to pick its agent class.
            "OPENSCIENTIST_PROVIDER",
            "OPENSCIENTIST_HARNESS",
        ],
    )
    def test_core_env_keys_allowed(self, tmp_path: Path, key: str) -> None:
        assert key in AGENT_ENV_ALLOWLIST

    @pytest.mark.parametrize(
        "key",
        [
            # The provider credentials the web keeps behind its LLM proxy,
            # the chat turn the launcher injects itself, and the harness env
            # key the container's own agent builds.
            "OPENSCIENTIST_CHAT_TURN",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_PROFILE",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "CODEX_AUTH_HOST_PATH",
            "ANTHROPIC_FOUNDRY_RESOURCE",
            "OPENAI_BASE_URL",
        ],
    )
    def test_web_held_env_keys_refused(self, tmp_path: Path, key: str) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError, match=r"env key .+ is not allowed"):
            validate_agent_request(_agent_request(env={key: "x"}), cfg)

    @pytest.mark.parametrize("memory", [8193, 0, -1])
    def test_memory_mib_out_of_bounds_refused(self, tmp_path: Path, memory: int) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(memory_mib=memory), cfg)

    @pytest.mark.parametrize("cpu", [4.1, 0, -1])
    def test_cpu_out_of_bounds_refused(self, tmp_path: Path, cpu: float) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(cpu=cpu), cfg)

    @pytest.mark.parametrize("memory", [True, "4096"])
    def test_memory_mib_wrong_type_refused(self, tmp_path: Path, memory: object) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(memory_mib=memory), cfg)

    def test_run_home_outside_jobs_root_refused(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "home").mkdir()
        cfg = _config(jobs)
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(run_home={"path": str(outside / "home")}), cfg)

    def test_run_home_unknown_field_refused(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        home = jobs / ".run-homes" / LOGIN_ROLE
        home.mkdir(parents=True)
        cfg = _config(jobs)
        with pytest.raises(ValidationError):
            validate_agent_request(
                _agent_request(run_home={"path": str(home), "name": ".codex"}), cfg
            )

    def test_run_home_valid(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        home = jobs / ".run-homes" / LOGIN_ROLE
        home.mkdir(parents=True)
        cfg = _config(jobs)
        launch = validate_agent_request(_agent_request(run_home={"path": str(home)}), cfg)
        assert launch.run_home == home

    def test_phenix_flag_without_configured_path_refused(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(phenix=True), cfg)

    @pytest.mark.parametrize("airgapped", [False, True])
    def test_egress_required_in_every_mode(self, tmp_path: Path, airgapped: bool) -> None:
        cfg = _config(_jobs(tmp_path))
        body = _agent_request(airgapped=airgapped)
        body.pop("egress_allow")
        with pytest.raises(ValidationError, match="egress_allow is required"):
            validate_agent_request(body, cfg)

    @pytest.mark.parametrize(
        "entry",
        ["db :5432", "db\n:5432", "nft add table x", "db:5432:extra", "db:", ":5432", "db:543216"],
    )
    def test_bad_egress_entry_refused(self, tmp_path: Path, entry: str) -> None:
        cfg = _config(_jobs(tmp_path))
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(airgapped=True, egress_allow=[entry]), cfg)

    def test_egress_above_entry_cap_refused(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        entries = [f"host{i}:443" for i in range(9)]
        with pytest.raises(ValidationError):
            validate_agent_request(_agent_request(airgapped=True, egress_allow=entries), cfg)

    def test_valid_egress_passes(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        launch = validate_agent_request(
            _agent_request(airgapped=True, egress_allow=["postgres:5432", "api.openai.com:443"]),
            cfg,
        )
        assert launch.airgapped is True
        assert launch.egress_allow == ("postgres:5432", "api.openai.com:443")


class TestExecutorRequestValidation:
    def _request(self, jobs: Path, **input_overrides) -> dict[str, object]:
        input_body: dict[str, object] = {
            "code": "print(1)",
            "language": "python",
            "data_path": None,
            "output_dir": str(jobs / JOB_ID / "out"),
            "timeout": 30,
            "description": "test",
            "iteration": 0,
            "data_files": [{"path": str(jobs / JOB_ID / "data.csv"), "name": "data.csv"}],
        }
        input_body.update(input_overrides)
        return {
            "job_id": JOB_ID,
            "timeout_seconds": 60,
            "memory_mib": 1024,
            "cpu": 0.5,
            "egress_allow": ["openscientist:8081", "openscientist:8082"],
            "input": input_body,
        }

    def _jobs_with_data(self, tmp_path: Path) -> Path:
        jobs = _jobs(tmp_path)
        (jobs / JOB_ID / "out").mkdir()
        (jobs / JOB_ID / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        return jobs

    def test_valid_request_passes(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        req = validate_exec_request(self._request(jobs), _config(jobs))
        assert req.job_id == JOB_ID
        assert req.timeout_seconds == 60
        assert req.output_dir == jobs / JOB_ID / "out"

    def test_output_dir_outside_job_dir_refused(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        outside = tmp_path / "out"
        outside.mkdir()
        with pytest.raises(ValidationError):
            validate_exec_request(self._request(jobs, output_dir=str(outside)), _config(jobs))

    def test_data_file_outside_job_dir_refused(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        stray = tmp_path / "stray.csv"
        stray.write_text("x", encoding="utf-8")
        with pytest.raises(ValidationError):
            validate_exec_request(
                self._request(jobs, data_files=[{"path": str(stray)}]), _config(jobs)
            )

    def test_missing_data_file_refused(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        with pytest.raises(ValidationError):
            validate_exec_request(
                self._request(jobs, data_files=[{"path": str(jobs / JOB_ID / "gone.csv")}]),
                _config(jobs),
            )

    def test_a_path_climbing_out_of_the_job_dir_is_refused(self, tmp_path: Path) -> None:
        """The check is on the path as written, since the executor resolves
        symlinks inside its own mount of the job directory."""
        jobs = self._jobs_with_data(tmp_path)
        (tmp_path / "other").mkdir()
        climbing = str(jobs / JOB_ID / "out" / ".." / ".." / ".." / "other")
        with pytest.raises(ValidationError):
            validate_exec_request(self._request(jobs, output_dir=climbing), _config(jobs))

    @pytest.mark.parametrize("field", ["privileged", "cap_add", "image", "volumes"])
    def test_unknown_top_level_field_refused(self, tmp_path: Path, field: str) -> None:
        jobs = self._jobs_with_data(tmp_path)
        with pytest.raises(ValidationError):
            validate_exec_request(self._request(jobs, **{field: "injected"}), _config(jobs))

    @pytest.mark.parametrize("field", ["privileged", "extra_field", "network"])
    def test_unknown_input_field_refused(self, tmp_path: Path, field: str) -> None:
        jobs = self._jobs_with_data(tmp_path)
        body = self._request(jobs)
        assert isinstance(body["input"], dict)
        body["input"][field] = "extra"  # type: ignore[index]
        with pytest.raises(ValidationError):
            validate_exec_request(body, _config(jobs))

    @pytest.mark.parametrize("timeout", [0, -1, "60"])
    def test_timeout_seconds_out_of_bounds(self, tmp_path: Path, timeout: object) -> None:
        jobs = self._jobs_with_data(tmp_path)
        with pytest.raises(ValidationError):
            validate_exec_request(
                self._request(jobs, **{"timeout_seconds": timeout}),
                _config(jobs),  # type: ignore[arg-type]
            )

    def test_memory_above_executor_bound_refused(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        with pytest.raises(ValidationError):
            validate_exec_request(self._request(jobs, **{"memory_mib": 2049}), _config(jobs))

    def test_non_string_code_refused(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        with pytest.raises(ValidationError):
            validate_exec_request(self._request(jobs, code=7), _config(jobs))

    def test_negative_iteration_refused(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        with pytest.raises(ValidationError):
            validate_exec_request(self._request(jobs, iteration=-1), _config(jobs))

    def test_data_files_entry_without_path_refused(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        with pytest.raises(ValidationError):
            validate_exec_request(
                self._request(jobs, data_files=[{"name": "no-path.csv"}]), _config(jobs)
            )

    def test_egress_allow_required_when_not_airgapped(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        body = self._request(jobs)
        body.pop("egress_allow")
        with pytest.raises(ValidationError, match="required when not airgapped"):
            validate_exec_request(body, _config(jobs))

    def test_egress_allow_refused_when_airgapped(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        body = self._request(jobs)
        body["airgapped"] = True
        with pytest.raises(ValidationError, match="not allowed when airgapped"):
            validate_exec_request(body, _config(jobs))

    def test_airgapped_request_without_egress_passes(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        body = self._request(jobs)
        body["airgapped"] = True
        body.pop("egress_allow")
        req = validate_exec_request(body, _config(jobs))
        assert req.airgapped is True
        assert req.egress_allow == ()

    @pytest.mark.parametrize("entry", ["db :5432", "db\n:5432", "nft add table x", "db:5432:extra"])
    def test_bad_egress_entry_refused(self, tmp_path: Path, entry: str) -> None:
        jobs = self._jobs_with_data(tmp_path)
        body = self._request(jobs)
        body["egress_allow"] = [entry]
        with pytest.raises(ValidationError, match="not a valid host:port"):
            validate_exec_request(body, _config(jobs))

    def test_egress_above_entry_cap_refused(self, tmp_path: Path) -> None:
        jobs = self._jobs_with_data(tmp_path)
        body = self._request(jobs)
        body["egress_allow"] = [f"host:{i}" for i in range(9)]
        with pytest.raises(ValidationError, match="entry bound"):
            validate_exec_request(body, _config(jobs))


def _binds(kwargs: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    """Each mount's source with its target and read-only flag; every one is a
    bind mount, which the daemon refuses for a source missing on the host."""
    assert "volumes" not in kwargs
    assert all(mount["Type"] == "bind" for mount in kwargs["mounts"])
    return {mount["Source"]: (mount["Target"], mount["ReadOnly"]) for mount in kwargs["mounts"]}


def test_airgap_hosts_pin_each_ipv4_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def resolve(host: str, port: None, family: int, kind: int):
        calls.append(host)
        assert (port, family, kind) == (None, socket.AF_INET, socket.SOCK_STREAM)
        if host == "gone.invalid":
            raise socket.gaierror(socket.EAI_NONAME, "No address")
        return [
            (family, kind, 6, "", ("172.26.0.3", 0)),
            (family, kind, 6, "", ("172.26.0.2", 0)),
            (family, kind, 6, "", ("172.26.0.3", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert _airgap_hosts(
        ("web:8081", "web:8082", "gone.invalid:443", "host.docker.internal:11434"),
        {"host.docker.internal": "host-gateway"},
    ) == [
        "host.docker.internal:host-gateway",
        "web:172.26.0.2",
        "web:172.26.0.3",
    ]
    assert calls == ["web", "gone.invalid"]


class TestAgentTemplate:
    def test_non_airgap_template(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        launch = validate_agent_request(_agent_request(), cfg)
        kwargs = agent_run_kwargs(cfg, launch, network="bridge")
        assert kwargs["image"] == "agent:latest"
        assert kwargs["name"].startswith(f"openscientist-agent-{JOB_ID[:12]}")
        assert kwargs["network"] == "bridge"
        assert kwargs["mem_limit"] == "4096m"
        assert kwargs["nano_cpus"] == 2_000_000_000
        assert kwargs["security_opt"] == ["no-new-privileges:true"]
        assert kwargs["extra_hosts"] == {"host.docker.internal": "host-gateway"}
        assert _binds(kwargs) == {str(cfg.jobs_root / JOB_ID): (f"/agent/jobs/{JOB_ID}", False)}
        labels = kwargs["labels"]
        assert labels["openscientist.job_id"] == JOB_ID
        assert labels["openscientist.type"] == "agent"
        assert labels["openscientist.launcher"] == "test"
        assert labels["openscientist.db_login"] == LOGIN_ROLE
        # The open agent starts behind the restrict firewall with the web's
        # allow-list.
        assert kwargs["cap_add"] == ["NET_ADMIN"]
        assert kwargs["user"] == "root"
        assert kwargs["entrypoint"] == ["/agent-firewall-entrypoint.sh"]
        assert (
            kwargs["environment"]["OPENSCIENTIST_FIREWALL_ALLOW"]
            == "openscientist:8081,openscientist:8082"
        )
        assert kwargs["environment"]["OPENSCIENTIST_FIREWALL_MODE"] == "restrict"
        assert "OPENSCIENTIST_FIREWALL_RUN_AS" not in kwargs["environment"]

    def test_chat_mode_template(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        launch = validate_agent_request(_agent_request(mode="chat", chat_turn="hi"), cfg)
        kwargs = agent_run_kwargs(cfg, launch, network="bridge")
        assert kwargs["name"].startswith(f"openscientist-chat-{JOB_ID[:12]}")
        assert kwargs["labels"]["openscientist.type"] == "chat"
        assert kwargs["environment"]["OPENSCIENTIST_CHAT_TURN"] == "hi"

    def test_airgapped_template(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        launch = validate_agent_request(
            _agent_request(airgapped=True, egress_allow=["postgres:5432", "api.openai.com:443"]),
            cfg,
        )
        kwargs = agent_run_kwargs(cfg, launch, network="bridge")
        assert kwargs["cap_add"] == ["NET_ADMIN"]
        assert kwargs["user"] == "root"
        assert kwargs["entrypoint"] == ["/agent-firewall-entrypoint.sh"]
        assert (
            kwargs["environment"]["OPENSCIENTIST_FIREWALL_ALLOW"]
            == "postgres:5432,api.openai.com:443"
        )
        assert kwargs["environment"]["OPENSCIENTIST_FIREWALL_MODE"] == "airgap"
        assert "OPENSCIENTIST_FIREWALL_RUN_AS" not in kwargs["environment"]

    def test_operator_mounts(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        cfg = _config(jobs, phenix_host_path="/srv/phenix")
        home = jobs / ".run-homes" / LOGIN_ROLE
        home.mkdir(parents=True)
        launch = validate_agent_request(
            _agent_request(
                phenix=True,
                run_home={"path": str(home)},
            ),
            cfg,
        )
        kwargs = agent_run_kwargs(cfg, launch, network="bridge")
        binds = _binds(kwargs)
        assert binds["/srv/phenix"] == ("/opt/phenix", True)
        assert binds[str(home)] == ("/agent/run-home", False)
        assert kwargs["environment"]["OPENSCIENTIST_RUN_HOME"] == "/agent/run-home"
        assert kwargs["labels"]["openscientist.run_home"] == str(home)

    def test_the_run_home_env_is_absent_without_a_home(self, tmp_path: Path) -> None:
        cfg = _config(_jobs(tmp_path))
        launch = validate_agent_request(_agent_request(), cfg)
        kwargs = agent_run_kwargs(cfg, launch, network="bridge")
        assert "OPENSCIENTIST_RUN_HOME" not in kwargs["environment"]


class TestSymlinkedPathsRefused:
    """The daemon follows a symlink it is given as a bind source, so the
    launcher refuses to hand one to it."""

    def _symlinked_job_dir(self, tmp_path: Path) -> Path:
        jobs = _jobs(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (jobs / JOB_ID).rmdir()
        (jobs / JOB_ID).symlink_to(outside, target_is_directory=True)
        return jobs

    def test_symlinked_job_directory_refused(self, tmp_path: Path) -> None:
        jobs = self._symlinked_job_dir(tmp_path)
        with pytest.raises(ValidationError, match="not a real directory"):
            validate_agent_request(_agent_request(), _config(jobs))

    def test_symlinked_job_directory_refused_for_exec(self, tmp_path: Path) -> None:
        jobs = self._symlinked_job_dir(tmp_path)
        body = {
            "job_id": JOB_ID,
            "timeout_seconds": 60,
            "memory_mib": 1024,
            "cpu": 0.5,
            "egress_allow": ["openscientist:8081"],
            "input": {
                "code": "print(1)",
                "language": "python",
                "output_dir": str(jobs / JOB_ID / "out"),
                "timeout": 30,
                "iteration": 0,
            },
        }
        with pytest.raises(ValidationError, match="not a real directory"):
            validate_exec_request(body, _config(jobs))

    def test_run_home_under_a_symlinked_component_refused(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        job = jobs / JOB_ID
        (job / "real" / "home").mkdir(parents=True)
        (job / "link").symlink_to("real", target_is_directory=True)
        cfg = _config(jobs)
        with pytest.raises(ValidationError, match="symlink"):
            validate_agent_request(
                _agent_request(run_home={"path": str(job / "link" / "home")}), cfg
            )

    def test_run_home_that_is_a_symlink_refused(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        job = jobs / JOB_ID
        (job / "real-home").mkdir()
        (job / "link").symlink_to("real-home", target_is_directory=True)
        cfg = _config(jobs)
        with pytest.raises(ValidationError, match="symlink"):
            validate_agent_request(_agent_request(run_home={"path": str(job / "link")}), cfg)

    def test_symlinked_jobs_root_refused_at_startup(self, tmp_path: Path) -> None:
        real = tmp_path / "jobs"
        real.mkdir()
        root = tmp_path / "jobs-link"
        root.symlink_to(real, target_is_directory=True)
        problem = jobs_root_problem(_config(root), None)
        assert problem is not None and "symlink" in problem

    def test_real_jobs_root_passes_the_startup_check(self, tmp_path: Path) -> None:
        assert jobs_root_problem(_config(_jobs(tmp_path)), None) is None


def _executor_body(jobs: Path) -> dict[str, object]:
    (jobs / JOB_ID / "out").mkdir()
    (jobs / JOB_ID / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    return {
        "job_id": JOB_ID,
        "timeout_seconds": 60,
        "memory_mib": 1024,
        "cpu": 0.5,
        "egress_allow": ["openscientist:8081", "openscientist:8082"],
        "input": {
            "code": "print(1)",
            "language": "python",
            "data_path": str(jobs / JOB_ID / "data.csv"),
            "output_dir": str(jobs / JOB_ID / "out"),
            "timeout": 30,
            "description": "test",
            "iteration": 0,
            "data_files": [{"path": str(jobs / JOB_ID / "data.csv"), "name": "data.csv"}],
        },
    }


class TestExecutorTemplate:
    @pytest.mark.parametrize("limit", [1, 128, 512])
    def test_executor_process_limit(self, tmp_path: Path, limit: int) -> None:
        jobs = _jobs(tmp_path)
        cfg = _config(jobs, executor_max_pids=512)
        body = _executor_body(jobs)
        body["pids_limit"] = limit
        req = validate_exec_request(body, cfg)
        kwargs = executor_run_kwargs(cfg, req, network="bridge")
        assert kwargs["pids_limit"] == limit

    @pytest.mark.parametrize("agent", [True, False])
    def test_process_limit_respects_operator_bound(self, tmp_path: Path, agent: bool) -> None:
        jobs = _jobs(tmp_path)
        cfg = _config(jobs, agent_max_pids=32, executor_max_pids=32)
        body = _agent_request() if agent else _executor_body(jobs)
        validate = validate_agent_request if agent else validate_exec_request
        assert validate(body, cfg).pids_limit == 32
        body["pids_limit"] = 33
        with pytest.raises(ValidationError, match="pids_limit exceeds"):
            validate(body, cfg)

    def test_template_remaps_paths_and_builds_command(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        cfg = _config(jobs)
        req = validate_exec_request(_executor_body(jobs), cfg)
        kwargs = executor_run_kwargs(cfg, req, network="bridge")
        assert kwargs["image"] == "executor:latest"
        assert kwargs["name"].startswith(f"openscientist-exec-{JOB_ID}")
        # The open-network executor starts behind the restrict firewall.
        assert kwargs["user"] == "root"
        assert kwargs["cap_add"] == ["NET_ADMIN"]
        assert kwargs["entrypoint"] == ["/agent-firewall-entrypoint.sh"]
        assert kwargs["environment"] == {
            "OPENSCIENTIST_FIREWALL_ALLOW": "openscientist:8081,openscientist:8082",
            "OPENSCIENTIST_FIREWALL_MODE": "restrict",
            "OPENSCIENTIST_FIREWALL_RUN_AS": "executor",
        }
        assert kwargs["stop_signal"] == "SIGKILL"
        assert kwargs["security_opt"] == ["no-new-privileges:true"]
        assert kwargs["network"] == "bridge"
        assert kwargs["mem_limit"] == "1024m"
        assert kwargs["nano_cpus"] == 500_000_000
        assert kwargs["labels"]["openscientist.type"] == "executor"
        # The job directory is the only bind source, at the path the agent sees it.
        job_mount = f"/agent/jobs/{JOB_ID}"
        assert _binds(kwargs) == {str(jobs / JOB_ID): (job_mount, False)}
        command = kwargs["command"]
        assert command[:2] == ["sh", "-c"]
        assert command[2].startswith("echo ")
        b64 = command[2].split("echo ", 1)[1].split(" | base64 -d |", 1)[0]
        payload = json.loads(base64.b64decode(b64).decode())
        assert payload["output_dir"] == f"{job_mount}/out"
        assert payload["data_path"] == f"{job_mount}/data.csv"
        assert payload["data_files"] == [{"path": f"{job_mount}/data.csv", "name": "data.csv"}]
        assert payload["code"] == "print(1)"

    def test_airgapped_executor_uses_no_network(self, tmp_path: Path) -> None:
        jobs = _jobs(tmp_path)
        cfg = _config(jobs)
        body = _executor_body(jobs)
        body["airgapped"] = True
        body.pop("egress_allow")
        req = validate_exec_request(body, cfg)
        kwargs = executor_run_kwargs(cfg, req, network="bridge")
        assert kwargs["network"] == "none"
        # With no network there is nothing to filter, so the executor keeps
        # its own user and entrypoint and takes no firewall environment.
        assert kwargs["user"] == "executor"
        assert "cap_add" not in kwargs
        assert kwargs["entrypoint"] == []
        assert "environment" not in kwargs
