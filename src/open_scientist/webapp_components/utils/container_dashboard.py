"""Container dashboard data collection for the admin page.

Queries Docker API and database to build a real-time view of all
Open Scientist-managed containers (agent + executor) grouped by job.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ContainerInfo:
    """Information about a single Docker container."""

    container_id: str
    name: str
    container_type: str  # "agent" or "executor"
    job_id: str | None
    status: str  # Docker status: running, exited, created, restarting, dead, removing
    created_at: datetime | None
    uptime_seconds: float = 0.0
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    memory_limit_mb: float = 0.0


@dataclass
class JobContainerGroup:
    """All containers associated with a single job, plus DB metadata."""

    job_id: str
    title: str
    status: str
    owner_email: str
    current_iteration: int
    max_iterations: int
    agent_container: ContainerInfo | None = None
    executor_containers: list[ContainerInfo] = field(default_factory=list)


@dataclass
class DashboardTotals:
    """Aggregate stats for the summary row."""

    running_jobs: int = 0
    agent_containers: int = 0
    executor_containers: int = 0
    total_memory_mb: float = 0.0
    total_cpu_percent: float = 0.0


@dataclass
class DashboardData:
    """Complete payload for the containers dashboard."""

    docker_available: bool = False
    job_groups: list[JobContainerGroup] = field(default_factory=list)
    orphan_containers: list[ContainerInfo] = field(default_factory=list)
    totals: DashboardTotals = field(default_factory=DashboardTotals)
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Docker helpers (run in threads — Docker SDK is blocking)
# ---------------------------------------------------------------------------


def _get_docker_client() -> Any:
    """Create a Docker client; returns None if Docker is unavailable."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return client
    except Exception:
        logger.debug("Docker client unavailable", exc_info=True)
        return None


def _list_open_scientist_containers(client: Any) -> list[Any]:
    """List all containers with a ``open_scientist.type`` label."""
    return list(
        client.containers.list(
            all=True,
            filters={"label": ["open_scientist.type"]},
        )
    )


def _get_container_stats(container: Any) -> dict[str, Any]:
    """Fetch one-shot stats for a container (blocks ~200ms)."""
    try:
        return dict(container.stats(stream=False))
    except Exception:
        logger.debug("Failed to fetch stats for container %s", container.name, exc_info=True)
        return {}


def _parse_container_stats(stats: dict[str, Any]) -> tuple[float, float, float]:
    """Extract CPU %, memory MB, and memory limit MB from raw Docker stats.

    Returns:
        (cpu_percent, memory_mb, memory_limit_mb)
    """
    cpu_percent = 0.0
    memory_mb = 0.0
    memory_limit_mb = 0.0

    # CPU calculation
    cpu_stats = stats.get("cpu_stats", {})
    precpu_stats = stats.get("precpu_stats", {})

    cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - precpu_stats.get(
        "cpu_usage", {}
    ).get("total_usage", 0)
    system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get("system_cpu_usage", 0)

    if system_delta > 0 and cpu_delta >= 0:
        num_cpus = len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", []) or [])
        if num_cpus == 0:
            num_cpus = cpu_stats.get("online_cpus", 1)
        cpu_percent = (cpu_delta / system_delta) * num_cpus * 100.0

    # Memory calculation
    mem_stats = stats.get("memory_stats", {})
    usage = mem_stats.get("usage", 0)
    cache = mem_stats.get("stats", {}).get("cache", 0)
    memory_mb = (usage - cache) / (1024 * 1024)
    if memory_mb < 0:
        memory_mb = usage / (1024 * 1024)

    memory_limit_mb = mem_stats.get("limit", 0) / (1024 * 1024)

    return cpu_percent, memory_mb, memory_limit_mb


def _build_container_info(
    container: Any,
    stats: dict[str, Any] | None = None,
) -> ContainerInfo:
    """Build a ContainerInfo from a Docker container object."""
    labels = container.labels or {}
    created_at = None
    uptime_seconds = 0.0

    created_str = container.attrs.get("Created", "")
    if created_str:
        try:
            created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if container.status == "running":
                uptime_seconds = (datetime.now(UTC) - created_at).total_seconds()
        except (ValueError, TypeError):
            pass

    cpu_percent = 0.0
    memory_mb = 0.0
    memory_limit_mb = 0.0
    if stats:
        cpu_percent, memory_mb, memory_limit_mb = _parse_container_stats(stats)

    return ContainerInfo(
        container_id=container.short_id,
        name=container.name,
        container_type=labels.get("open_scientist.type", "unknown"),
        job_id=labels.get("open_scientist.job_id"),
        status=container.status,
        created_at=created_at,
        uptime_seconds=uptime_seconds,
        cpu_percent=cpu_percent,
        memory_mb=memory_mb,
        memory_limit_mb=memory_limit_mb,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def collect_dashboard_data(include_stats: bool = True) -> DashboardData:
    """Collect all data needed for the container dashboard.

    Args:
        include_stats: Whether to fetch per-container CPU/memory stats.
            Set to False for faster polling when stats aren't needed.

    Returns:
        DashboardData with containers grouped by job.
    """
    data = DashboardData()

    ready, client = await _prepare_dashboard_client(data)
    if not ready:
        return data

    fetch_result = await _fetch_dashboard_sources(client)
    if fetch_result is None:
        data.error_message = "Error collecting container data. Check server logs."
        return data
    containers_list, job_map = fetch_result

    stats_map = await _collect_running_container_stats(containers_list, include_stats)
    container_infos = _build_container_infos(containers_list, stats_map)
    data.job_groups, data.orphan_containers = _group_containers_by_job(container_infos, job_map)
    data.totals = _compute_dashboard_totals(data.job_groups, data.orphan_containers)
    return data


async def _prepare_dashboard_client(data: DashboardData) -> tuple[bool, Any | None]:
    client = await asyncio.to_thread(_get_docker_client)
    if client is None:
        data.error_message = "Docker daemon is not reachable."
        return False, None

    data.docker_available = True
    return True, client


async def _fetch_dashboard_sources(
    client: Any,
) -> tuple[list[Any], dict[str, dict[str, Any]]] | None:
    try:
        containers_list, job_map = await asyncio.gather(
            asyncio.to_thread(_list_open_scientist_containers, client),
            _get_active_jobs_map(),
        )
    except Exception as exc:
        logger.error("Failed to collect dashboard data: %s", exc, exc_info=True)
        return None
    return containers_list, job_map


async def _collect_running_container_stats(
    containers_list: list[Any],
    include_stats: bool,
) -> dict[str, dict[str, Any]]:
    stats_map: dict[str, dict[str, Any]] = {}
    if not include_stats or not containers_list:
        return stats_map

    running = [c for c in containers_list if c.status == "running"]
    if not running:
        return stats_map

    with ThreadPoolExecutor(max_workers=min(len(running), 8)) as pool:
        loop = asyncio.get_running_loop()
        futures = {c.short_id: loop.run_in_executor(pool, _get_container_stats, c) for c in running}
        for cid, fut in futures.items():
            try:
                stats_map[cid] = await fut
            except Exception:
                stats_map[cid] = {}
    return stats_map


def _build_container_infos(
    containers_list: list[Any], stats_map: dict[str, dict[str, Any]]
) -> list[ContainerInfo]:
    return [_build_container_info(c, stats_map.get(c.short_id)) for c in containers_list]


def _build_group(job_id: str, job_info: dict[str, Any]) -> JobContainerGroup:
    return JobContainerGroup(
        job_id=job_id,
        title=job_info["title"],
        status=job_info["status"],
        owner_email=job_info["owner_email"],
        current_iteration=job_info["current_iteration"],
        max_iterations=job_info["max_iterations"],
    )


def _group_containers_by_job(
    container_infos: list[ContainerInfo],
    job_map: dict[str, dict[str, Any]],
) -> tuple[list[JobContainerGroup], list[ContainerInfo]]:
    groups: dict[str, JobContainerGroup] = {}
    orphans: list[ContainerInfo] = []

    for ci in container_infos:
        if ci.job_id is None:
            orphans.append(ci)
            continue

        group = groups.get(ci.job_id)
        if group is None:
            job_info = job_map.get(ci.job_id)
            if job_info is None:
                orphans.append(ci)
                continue
            group = _build_group(ci.job_id, job_info)
            groups[ci.job_id] = group

        if ci.container_type == "agent":
            group.agent_container = ci
        else:
            group.executor_containers.append(ci)

    return list(groups.values()), orphans


def _compute_dashboard_totals(
    job_groups: list[JobContainerGroup],
    orphan_containers: list[ContainerInfo],
) -> DashboardTotals:
    totals = DashboardTotals(running_jobs=len(job_groups))
    for group in job_groups:
        if group.agent_container:
            totals.agent_containers += 1
            totals.total_memory_mb += group.agent_container.memory_mb
            totals.total_cpu_percent += group.agent_container.cpu_percent
        for ec in group.executor_containers:
            totals.executor_containers += 1
            totals.total_memory_mb += ec.memory_mb
            totals.total_cpu_percent += ec.cpu_percent
    for orphan in orphan_containers:
        totals.total_memory_mb += orphan.memory_mb
        totals.total_cpu_percent += orphan.cpu_percent
    return totals


async def _get_active_jobs_map() -> dict[str, dict[str, Any]]:
    """Query the database for running/queued/pending jobs.

    Returns:
        Mapping from job_id (str) to a dict with title, status, etc.
    """
    from sqlalchemy import select

    from open_scientist.database.models import Job, User
    from open_scientist.database.session import get_admin_session

    result_map: dict[str, dict[str, Any]] = {}

    try:
        async with get_admin_session() as session:
            stmt = (
                select(Job, User.email)
                .outerjoin(User, Job.owner_id == User.id)
                .where(Job.status.in_(["running", "queued", "pending"]))
            )
            rows = await session.execute(stmt)

            for job, email in rows:
                result_map[str(job.id)] = {
                    "title": job.title,
                    "status": job.status,
                    "owner_email": email or "unassigned",
                    "current_iteration": job.current_iteration,
                    "max_iterations": job.max_iterations,
                }
    except Exception as exc:
        logger.error("Failed to query active jobs: %s", exc, exc_info=True)

    return result_map
