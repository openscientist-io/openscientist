"""The two server-side templates: everything the web cannot choose is fixed here."""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path, PurePosixPath
from typing import Any

from docker.types import Mount

from openscientist_launcher.config import LauncherConfig
from openscientist_launcher.validation import AgentLaunch, ExecRequest

AGENT_APP_DIR = "/agent"
PHENIX_PATH = "/opt/phenix"
MARKER_LABEL = "openscientist.launcher"
FIREWALL_ENTRYPOINT = ["/agent-firewall-entrypoint.sh"]
NET_ADMIN = "NET_ADMIN"
FIREWALL_ALLOW_ENV = "OPENSCIENTIST_FIREWALL_ALLOW"
FIREWALL_MODE_ENV = "OPENSCIENTIST_FIREWALL_MODE"
FIREWALL_RUN_AS_ENV = "OPENSCIENTIST_FIREWALL_RUN_AS"
FIREWALL_MODE_AIRGAP = "airgap"
FIREWALL_MODE_RESTRICT = "restrict"


def _bind(source: str, target: str, *, read_only: bool) -> Mount:
    """A bind mount the daemon refuses when ``source`` is missing on the host,
    where a ``volumes`` entry would have it create an empty root-owned directory."""
    return Mount(target=target, source=source, type="bind", read_only=read_only)


def _job_mount(job_id: str) -> str:
    """Where a container sees its job directory, the same path in the agent and the executor."""
    return f"{AGENT_APP_DIR}/jobs/{job_id}"


def _labels(cfg: LauncherConfig, job_id: str, container_type: str) -> dict[str, str]:
    return {
        "openscientist.job_id": job_id,
        "openscientist.type": container_type,
        MARKER_LABEL: cfg.instance_id,
    }


def agent_run_kwargs(cfg: LauncherConfig, launch: AgentLaunch, *, network: str) -> dict[str, Any]:
    """The docker run kwargs of one agent or chat container."""
    if launch.mode == "chat":
        name = f"openscientist-chat-{launch.job_id[:12]}-{secrets.token_hex(4)}"
        container_type = "chat"
    else:
        name = f"openscientist-agent-{launch.job_id[:12]}"
        container_type = "agent"

    env = dict(launch.env)
    if launch.mode == "chat" and launch.chat_turn is not None:
        env["OPENSCIENTIST_CHAT_TURN"] = launch.chat_turn
    # The container starts behind the firewall in every mode, so the allow
    # list and its mode ride on every launch.
    env[FIREWALL_ALLOW_ENV] = ",".join(launch.egress_allow)
    env[FIREWALL_MODE_ENV] = FIREWALL_MODE_AIRGAP if launch.airgapped else FIREWALL_MODE_RESTRICT

    job_mount = _job_mount(launch.job_id)
    mounts = [_bind(str(cfg.jobs_root / launch.job_id), job_mount, read_only=False)]
    if launch.phenix and cfg.phenix_host_path:
        phenix = Path(cfg.phenix_host_path).expanduser().resolve()
        mounts.append(_bind(str(phenix), PHENIX_PATH, read_only=True))
    if launch.run_home is not None:
        home, name_in_job = launch.run_home
        mounts.append(_bind(str(home), f"{job_mount}/{name_in_job}", read_only=False))

    labels = _labels(cfg, launch.job_id, container_type)
    if launch.db_login:
        labels["openscientist.db_login"] = launch.db_login
    if launch.run_home is not None:
        labels["openscientist.run_home"] = str(launch.run_home[0])

    kwargs: dict[str, Any] = {
        "image": cfg.agent_image,
        "name": name,
        "detach": True,
        "environment": env,
        "mounts": mounts,
        "network": network,
        "mem_limit": f"{launch.memory_mib}m",
        "nano_cpus": int(launch.cpu * 1e9),
        "platform": cfg.agent_platform,
        "security_opt": ["no-new-privileges:true"],
        # The container reaches a host-local model endpoint through the gateway.
        "extra_hosts": {"host.docker.internal": "host-gateway"},
        "labels": labels,
    }
    # NET_ADMIN installs the firewall rules, then the entrypoint drops to the
    # agent user with it gone.
    kwargs["cap_add"] = [NET_ADMIN]
    kwargs["user"] = "root"
    kwargs["entrypoint"] = FIREWALL_ENTRYPOINT
    return kwargs


def executor_run_kwargs(cfg: LauncherConfig, req: ExecRequest, *, network: str) -> dict[str, Any]:
    """The docker run kwargs of one executor container, and its remapped payload."""
    name = f"openscientist-exec-{req.job_id}-{secrets.token_hex(4)}"

    # The job directory is the one path here the agent cannot replace, since
    # it is the root of the agent's own mount, so it is the only bind source.
    # The output and data paths are reached inside it, where a symlink the
    # agent plants resolves within the executor's filesystem, never the host.
    job_dir = cfg.jobs_root / req.job_id
    job_mount = PurePosixPath(_job_mount(req.job_id))
    mounts = [_bind(str(job_dir), str(job_mount), read_only=False)]

    def in_container(path: Path) -> str:
        return str(job_mount / path.relative_to(job_dir))

    payload = {
        "code": req.code,
        "language": req.language,
        "data_path": in_container(req.data_path) if req.data_path else None,
        "output_dir": in_container(req.output_dir),
        "timeout": req.payload_timeout,
        "description": req.description,
        "iteration": req.iteration,
        "data_files": [
            {**entry, "path": in_container(Path(str(entry["path"])))} for entry in req.data_files
        ],
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()

    kwargs: dict[str, Any] = {
        "image": cfg.executor_image,
        "name": name,
        "stdin_open": True,
        "detach": True,
        "network": "none" if req.airgapped else network,
        "mem_limit": f"{req.memory_mib}m",
        "nano_cpus": int(req.cpu * 1e9),
        "security_opt": ["no-new-privileges:true"],
        "user": "executor",
        "labels": _labels(cfg, req.job_id, "executor"),
        "mounts": mounts,
        "stop_signal": "SIGKILL",
        # The image entrypoint is cleared so the shell pipe is the whole command.
        "entrypoint": [],
        "command": ["sh", "-c", f"echo {encoded} | base64 -d | python -m openscientist_executor"],
    }
    if not req.airgapped:
        # The firewall installs the egress rules as root, then drops to the
        # executor user with NET_ADMIN gone, so it cannot undo them.
        kwargs["cap_add"] = [NET_ADMIN]
        kwargs["user"] = "root"
        kwargs["entrypoint"] = FIREWALL_ENTRYPOINT
        kwargs["environment"] = {
            FIREWALL_ALLOW_ENV: ",".join(req.egress_allow),
            FIREWALL_MODE_ENV: FIREWALL_MODE_RESTRICT,
            FIREWALL_RUN_AS_ENV: "executor",
        }
    return kwargs
