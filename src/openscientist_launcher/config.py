"""The launcher's configuration, read entirely from its environment."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class ConfigError(Exception):
    """The launcher's environment is missing or malformed; it must not start."""


def _require(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "")
    if not value:
        raise ConfigError(f"{key} is not set")
    return value


def _int(env: Mapping[str, str], key: str, *, default: str | None = None) -> int:
    raw = env.get(key, "") if env.get(key) else default
    if raw is None:
        raise ConfigError(f"{key} is not set")
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} is not an integer") from exc


def _float(env: Mapping[str, str], key: str) -> float:
    raw = env.get(key, "")
    if not raw:
        raise ConfigError(f"{key} is not set")
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} is not a number") from exc


def _digests(env: Mapping[str, str], key: str) -> frozenset[str]:
    raw = _require(env, key)
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if not entries or any(not _DIGEST.match(entry) for entry in entries):
        raise ConfigError(f"{key} is not a comma-separated set of sha256 digests")
    return frozenset(entries)


@dataclass(frozen=True)
class LauncherConfig:
    """Every bound the launcher enforces comes from here, never from a request."""

    jobs_root: Path
    agent_image: str
    executor_image: str
    agent_image_digests: frozenset[str]
    executor_image_digests: frozenset[str]
    instance_id: str
    web_uid: int
    web_gid: int
    socket_path: Path
    network: str | None
    agent_platform: str | None
    phenix_host_path: str | None
    agent_max_memory_mib: int
    agent_max_cpu: float
    executor_max_memory_mib: int
    executor_max_cpu: float
    max_lifetime_seconds: int
    max_egress_entries: int
    agent_max_pids: int = 4096
    executor_max_pids: int = 4096

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LauncherConfig":
        env = dict(os.environ if env is None else env)
        web_uid = _int(env, "LAUNCHER_WEB_UID", default="0")
        web_gid = _int(env, "LAUNCHER_WEB_GID", default="0")
        if web_uid < 0 or web_gid < 0:
            raise ConfigError("LAUNCHER_WEB_UID and LAUNCHER_WEB_GID must be non-negative")
        agent_max_memory_mib = _int(env, "AGENT_MAX_MEMORY_MIB")
        executor_max_memory_mib = _int(env, "EXECUTOR_MAX_MEMORY_MIB")
        max_lifetime_seconds = _int(env, "MAX_LIFETIME_SECONDS")
        max_egress_entries = _int(env, "MAX_EGRESS_ENTRIES", default="64")
        if agent_max_memory_mib <= 0 or executor_max_memory_mib <= 0:
            raise ConfigError("the memory bounds must be positive MiB values")
        if max_lifetime_seconds <= 0 or max_egress_entries <= 0:
            raise ConfigError("MAX_LIFETIME_SECONDS and MAX_EGRESS_ENTRIES must be positive")
        agent_max_cpu = _float(env, "AGENT_MAX_CPU")
        executor_max_cpu = _float(env, "EXECUTOR_MAX_CPU")
        if agent_max_cpu <= 0 or executor_max_cpu <= 0:
            raise ConfigError("the CPU bounds must be positive")
        agent_max_pids = _int(env, "AGENT_MAX_PIDS", default="4096")
        executor_max_pids = _int(env, "EXECUTOR_MAX_PIDS", default="4096")
        if agent_max_pids <= 0 or executor_max_pids <= 0:
            raise ConfigError("the PID bounds must be positive")
        return cls(
            jobs_root=Path(_require(env, "JOBS_ROOT")),
            agent_image=_require(env, "AGENT_IMAGE"),
            executor_image=_require(env, "EXECUTOR_IMAGE"),
            agent_image_digests=_digests(env, "AGENT_IMAGE_DIGESTS"),
            executor_image_digests=_digests(env, "EXECUTOR_IMAGE_DIGESTS"),
            instance_id=_require(env, "LAUNCHER_INSTANCE_ID"),
            web_uid=web_uid,
            web_gid=web_gid,
            socket_path=Path(env.get("LAUNCHER_SOCKET_PATH", "/run/launcher/launcher.sock")),
            network=env.get("LAUNCHER_NETWORK") or None,
            agent_platform=env.get("LAUNCHER_AGENT_PLATFORM") or None,
            phenix_host_path=env.get("PHENIX_HOST_PATH") or None,
            agent_max_memory_mib=agent_max_memory_mib,
            agent_max_cpu=agent_max_cpu,
            executor_max_memory_mib=executor_max_memory_mib,
            executor_max_cpu=executor_max_cpu,
            agent_max_pids=agent_max_pids,
            executor_max_pids=executor_max_pids,
            max_lifetime_seconds=max_lifetime_seconds,
            max_egress_entries=max_egress_entries,
        )
