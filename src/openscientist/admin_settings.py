"""The admin settings row, read at each use and seeded from the environment once."""

import math
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text

from openscientist.async_tasks import run_sync
from openscientist.database.models.admin_settings import AdminSettings
from openscientist.database.session import get_admin_session, get_session_ctx
from openscientist.docker_memory import parse_docker_memory_mib
from openscientist.settings import get_settings


class AdminSettingsMissingError(RuntimeError):
    """The admin settings row is missing, which the web seeds at startup."""


@dataclass(frozen=True)
class AdminSettingsValues:
    """The admin settings row as a running part reads it at each use."""

    max_job_cost_usd: float | None
    max_project_spend_total_usd: float | None
    max_project_spend_24h_usd: float | None
    warn_project_spend_24h_usd: float | None
    agent_cpu: float
    executor_cpu: float
    agent_memory_mib: int
    executor_memory_mib: int
    agent_timeout_seconds: int
    executor_timeout_seconds: int
    max_file_size_mb: int
    max_concurrent_jobs: int
    scheduler_window_hours: float
    airgapped: bool
    airgap_locked: bool
    session_duration_days: int
    updated_at: datetime
    updated_by: UUID | None


def _inf_or_none(value: float) -> float | None:
    """The row stores no limit as NULL, as the environment writes inf."""
    return None if math.isinf(value) else value


async def seed_admin_settings() -> None:
    """Insert the admin settings row from the environment when it is missing.

    The environment seeds the values once. From then on only the panel counts,
    apart from the airgap floor, which the reader applies at each read.
    """
    settings = get_settings()
    values = {
        "max_job_cost_usd": settings.budget.max_job_cost_usd,
        "max_project_spend_total_usd": _inf_or_none(settings.budget.max_project_spend_total_usd),
        "max_project_spend_24h_usd": _inf_or_none(settings.budget.max_project_spend_24h_usd),
        "warn_project_spend_24h_usd": _inf_or_none(settings.budget.warn_project_spend_24h_usd),
        "agent_cpu": settings.container.agent_cpu,
        "executor_cpu": settings.container.executor_cpu,
        "agent_memory_mib": parse_docker_memory_mib(
            settings.container.agent_memory, "OPENSCIENTIST_AGENT_MEMORY"
        ),
        "executor_memory_mib": parse_docker_memory_mib(
            settings.container.executor_memory, "OPENSCIENTIST_EXECUTOR_MEMORY"
        ),
        "agent_timeout_seconds": settings.container.agent_timeout,
        "executor_timeout_seconds": settings.container.executor_timeout,
        "max_file_size_mb": settings.file.max_file_size_mb,
        "max_concurrent_jobs": settings.max_concurrent_jobs,
        "scheduler_window_hours": settings.scheduler_window_hours,
        "airgapped": settings.airgap.enabled,
        "session_duration_days": settings.auth.session_duration_days,
    }
    async with get_admin_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO admin_settings (
                    max_job_cost_usd, max_project_spend_total_usd,
                    max_project_spend_24h_usd, warn_project_spend_24h_usd,
                    agent_cpu, executor_cpu, agent_memory_mib, executor_memory_mib,
                    agent_timeout_seconds, executor_timeout_seconds,
                    max_file_size_mb, max_concurrent_jobs, scheduler_window_hours,
                    airgapped, session_duration_days
                ) VALUES (
                    :max_job_cost_usd, :max_project_spend_total_usd,
                    :max_project_spend_24h_usd, :warn_project_spend_24h_usd,
                    :agent_cpu, :executor_cpu, :agent_memory_mib, :executor_memory_mib,
                    :agent_timeout_seconds, :executor_timeout_seconds,
                    :max_file_size_mb, :max_concurrent_jobs, :scheduler_window_hours,
                    :airgapped, :session_duration_days
                )
                ON CONFLICT DO NOTHING
                """
            ),
            values,
        )
        await session.commit()


async def current_admin_settings() -> AdminSettingsValues:
    """The admin settings row read at each use, with the airgap floor applied.

    ``airgapped`` is True when the row says so or OPENSCIENTIST_AIRGAPPED is true
    in the environment, and ``airgap_locked`` is True exactly when the environment
    forces it.

    Raises:
        AdminSettingsMissingError: the row is missing, which the web seeds at startup.
    """
    async with get_session_ctx() as session:
        row = await session.get(AdminSettings, True)
        if row is None:
            raise AdminSettingsMissingError(
                "the admin settings row is missing; the web seeds it at startup"
            )
        floor = get_settings().airgap.enabled
        return AdminSettingsValues(
            max_job_cost_usd=row.max_job_cost_usd,
            max_project_spend_total_usd=row.max_project_spend_total_usd,
            max_project_spend_24h_usd=row.max_project_spend_24h_usd,
            warn_project_spend_24h_usd=row.warn_project_spend_24h_usd,
            agent_cpu=row.agent_cpu,
            executor_cpu=row.executor_cpu,
            agent_memory_mib=row.agent_memory_mib,
            executor_memory_mib=row.executor_memory_mib,
            agent_timeout_seconds=row.agent_timeout_seconds,
            executor_timeout_seconds=row.executor_timeout_seconds,
            max_file_size_mb=row.max_file_size_mb,
            max_concurrent_jobs=row.max_concurrent_jobs,
            scheduler_window_hours=row.scheduler_window_hours,
            airgapped=row.airgapped or floor,
            airgap_locked=floor,
            session_duration_days=row.session_duration_days,
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )


def current_admin_settings_sync() -> AdminSettingsValues:
    """current_admin_settings() from a thread without a running loop, the way
    job_manager and container_manager call database code through run_sync."""
    return run_sync(current_admin_settings())
