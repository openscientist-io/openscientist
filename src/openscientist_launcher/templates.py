"""The two server-side templates: everything the web cannot choose is fixed here."""

from __future__ import annotations

import base64
import json
import secrets
from pathlib import Path
from typing import Any

from openscientist_launcher.config import LauncherConfig
from openscientist_launcher.validation import AgentLaunch, ExecRequest, ValidationError

AGENT_APP_DIR = "/agent"
AGENT_GCP_CREDENTIALS_PATH = f"{AGENT_APP_DIR}/gcp-credentials.json"
PHENIX_PATH = "/opt/phenix"
MARKER_LABEL = "openscientist.launcher"
FIREWALL_ENTRYPOINT = ["/agent-firewall-entrypoint.sh"]
NET_ADMIN = "NET_ADMIN"
FIREWALL_ALLOW_ENV = "OPENSCIENTIST_FIREWALL_ALLOW"
FIREWALL_MODE_ENV = "OPENSCIENTIST_FIREWALL_MODE"
FIREWALL_RUN_AS_ENV = "OPENSCIENTIST_FIREWALL_RUN_AS"
FIREWALL_MODE_RESTRICT = "restrict"


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
    if launch.airgapped:
        env[FIREWALL_ALLOW_ENV] = ",".join(launch.egress_allow)

    job_mount = f"{AGENT_APP_DIR}/jobs/{launch.job_id}"
    volumes: dict[str, dict[str, str]] = {
        str(cfg.jobs_root / launch.job_id): {"bind": job_mount, "mode": "rw"}
    }
    if launch.gcp_credentials and cfg.gcp_credentials_host_path:
        volumes[cfg.gcp_credentials_host_path] = {"bind": AGENT_GCP_CREDENTIALS_PATH, "mode": "ro"}
    if launch.phenix and cfg.phenix_host_path:
        phenix = Path(cfg.phenix_host_path).expanduser().resolve()
        volumes[str(phenix)] = {"bind": PHENIX_PATH, "mode": "ro"}
    if launch.run_home is not None:
        home, name_in_job = launch.run_home
        volumes[str(home)] = {"bind": f"{job_mount}/{name_in_job}", "mode": "rw"}

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
        "volumes": volumes,
        "network": network,
        "mem_limit": f"{launch.memory_mib}m",
        "nano_cpus": int(launch.cpu * 1e9),
        "platform": cfg.agent_platform,
        "security_opt": ["no-new-privileges:true"],
        # The container reaches a host-local model endpoint through the gateway.
        "extra_hosts": {"host.docker.internal": "host-gateway"},
        "labels": labels,
    }
    if launch.airgapped:
        # NET_ADMIN installs the firewall rules, then the entrypoint drops to the agent user with it gone.
        kwargs["cap_add"] = [NET_ADMIN]
        kwargs["user"] = "root"
        kwargs["entrypoint"] = FIREWALL_ENTRYPOINT
    return kwargs


def executor_run_kwargs(cfg: LauncherConfig, req: ExecRequest, *, network: str) -> dict[str, Any]:
    """The docker run kwargs of one executor container, and its remapped payload."""
    name = f"openscientist-exec-{req.job_id}-{secrets.token_hex(4)}"

    volumes: dict[str, dict[str, str]] = {str(req.output_dir): {"bind": "/output", "mode": "rw"}}
    host_to_container: dict[str, str] = {}
    for index, entry in enumerate(req.data_files):
        parent = str(Path(str(entry["path"])).resolve().parent)
        if parent not in host_to_container:
            host_to_container[parent] = "/data" if index == 0 else f"/data/{index}"
            volumes[parent] = {"bind": host_to_container[parent], "mode": "ro"}

    def remap(path_str: str) -> str:
        resolved = Path(path_str).resolve()
        container_dir = host_to_container.get(str(resolved.parent))
        if container_dir is None:
            raise ValidationError("a data path is not under any mounted data directory")
        return str(Path(container_dir) / resolved.name)

    payload = {
        "code": req.code,
        "language": req.language,
        "data_path": remap(str(req.data_path)) if req.data_path else None,
        "output_dir": "/output",
        "timeout": req.payload_timeout,
        "description": req.description,
        "iteration": req.iteration,
        "data_files": [{**entry, "path": remap(str(entry["path"]))} for entry in req.data_files],
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
        "volumes": volumes,
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
