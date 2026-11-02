"""Tests for the container dashboard data service."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from docker import errors
from openscientist.database.models import Job, User
from openscientist.webapp_components.ui_components import format_uptime
from openscientist.webapp_components.utils.container_dashboard import (
    _parse_container_stats,
    collect_dashboard_data,
)
from tests.helpers import fake_admin_session


class TestFormatUptime:
    def test_seconds_only(self):
        assert format_uptime(30) == "30s"

    def test_zero(self):
        assert format_uptime(0) == "0s"

    def test_negative(self):
        assert format_uptime(-5) == "0s"

    def test_minutes_and_seconds(self):
        assert format_uptime(90) == "1m 30s"

    def test_exact_minutes(self):
        assert format_uptime(120) == "2m"

    def test_hours_and_minutes(self):
        assert format_uptime(8100) == "2h 15m"

    def test_exact_hours(self):
        assert format_uptime(3600) == "1h"

    def test_just_under_a_minute(self):
        assert format_uptime(59) == "59s"

    def test_just_over_a_minute(self):
        assert format_uptime(61) == "1m 1s"


class TestParseContainerStats:
    def test_normal_stats(self):
        stats = {
            "cpu_stats": {
                "cpu_usage": {
                    "total_usage": 200_000_000,
                    "percpu_usage": [100_000_000, 100_000_000],
                },
                "system_cpu_usage": 1_000_000_000,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100_000_000},
                "system_cpu_usage": 500_000_000,
            },
            "memory_stats": {
                "usage": 200 * 1024 * 1024,  # 200 MB
                "stats": {"cache": 50 * 1024 * 1024},  # 50 MB cache
                "limit": 2 * 1024 * 1024 * 1024,  # 2 GB
            },
        }
        cpu, mem, limit = _parse_container_stats(stats)

        # CPU: (100M / 500M) * 2 * 100 = 40%
        assert cpu == pytest.approx(40.0)
        # Memory: (200 - 50) MB = 150 MB
        assert mem == pytest.approx(150.0)
        # Limit: 2 GB = 2048 MB
        assert limit == pytest.approx(2048.0)

    def test_zero_system_delta(self):
        """CPU should be 0 when system delta is 0 (no time elapsed)."""
        stats = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 100, "percpu_usage": [100]},
                "system_cpu_usage": 500,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 500,
            },
            "memory_stats": {"usage": 0, "stats": {}, "limit": 0},
        }
        cpu, _mem, _limit = _parse_container_stats(stats)
        assert cpu == 0.0

    def test_empty_stats(self):
        cpu, mem, limit = _parse_container_stats({})
        assert cpu == 0.0
        assert mem == 0.0
        assert limit == 0.0

    def test_online_cpus_fallback(self):
        """Use online_cpus when percpu_usage is missing."""
        stats = {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 200},
                "system_cpu_usage": 1000,
                "online_cpus": 4,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 500,
            },
            "memory_stats": {"usage": 0, "stats": {}, "limit": 0},
        }
        cpu, _, _ = _parse_container_stats(stats)
        # (100 / 500) * 4 * 100 = 80%
        assert cpu == pytest.approx(80.0)


def _fake_docker_container(
    short_id: str,
    name: str,
    labels: dict,
    status: str = "running",
) -> SimpleNamespace:
    """Stand in for a docker SDK container object (the daemon side of the boundary)."""
    return SimpleNamespace(
        short_id=short_id,
        name=name,
        labels=labels,
        status=status,
        attrs={"Created": "2026-01-15T10:00:00Z"},
    )


class TestCollectDashboardDockerDown:
    @pytest.mark.asyncio
    async def test_returns_unavailable_when_docker_down(self):
        """With the Docker daemon unreachable the dashboard reports unavailability and no groups."""
        down_client = MagicMock()
        down_client.ping.side_effect = errors.DockerException("Cannot connect to the Docker daemon")

        with patch("docker.from_env", return_value=down_client):
            data = await collect_dashboard_data()

        assert data.docker_available is False
        assert data.error_message is not None
        assert "Docker" in data.error_message
        assert data.job_groups == []


class TestCollectDashboardGrouping:
    @pytest.mark.asyncio
    async def test_groups_containers_by_job(
        self, db_session: AsyncSession, test_job: Job, test_user: User
    ):
        """Containers with the same job label are grouped with the job's real database metadata."""
        job_id = str(test_job.id)

        agent = _fake_docker_container(
            "abc123",
            "openscientist-agent-abc",
            {"openscientist.type": "agent", "openscientist.job_id": job_id},
        )
        executor = _fake_docker_container(
            "def456",
            "openscientist-exec-def",
            {"openscientist.type": "executor", "openscientist.job_id": job_id},
        )

        client = MagicMock()
        client.containers.list.return_value = [agent, executor]

        # _get_active_jobs_map imports get_admin_session at call time, so patch
        # the source module
        with (
            patch(
                "openscientist.database.session.get_admin_session",
                fake_admin_session(db_session),
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._get_docker_client",
                return_value=client,
            ),
        ):
            data = await collect_dashboard_data(include_stats=False)

        client.containers.list.assert_called_once_with(
            all=True, filters={"label": ["openscientist.type"]}
        )
        assert data.docker_available is True
        assert len(data.job_groups) == 1
        group = data.job_groups[0]
        assert group.job_id == job_id
        assert group.title == test_job.research_question
        assert group.status == test_job.status
        assert group.owner_email == test_user.email
        assert group.current_iteration == test_job.current_iteration
        assert group.max_iterations == test_job.max_iterations
        assert group.agent_container is not None
        assert group.agent_container.name == "openscientist-agent-abc"
        assert len(group.executor_containers) == 1
        assert group.executor_containers[0].name == "openscientist-exec-def"
        assert data.orphan_containers == []

    @pytest.mark.asyncio
    async def test_orphan_containers_detected(self, db_session: AsyncSession, test_job: Job):
        """A container whose job_id is not in the database is flagged as orphan, while a
        container of the real job is still grouped."""
        orphan_job_id = str(uuid4())

        orphan = _fake_docker_container(
            "xyz789",
            "openscientist-agent-xyz",
            {"openscientist.type": "agent", "openscientist.job_id": orphan_job_id},
            status="exited",
        )
        grouped = _fake_docker_container(
            "abc123",
            "openscientist-agent-abc",
            {"openscientist.type": "agent", "openscientist.job_id": str(test_job.id)},
        )

        client = MagicMock()
        client.containers.list.return_value = [orphan, grouped]

        with (
            patch(
                "openscientist.database.session.get_admin_session",
                fake_admin_session(db_session),
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._get_docker_client",
                return_value=client,
            ),
        ):
            data = await collect_dashboard_data(include_stats=False)

        assert len(data.orphan_containers) == 1
        orphan_info = data.orphan_containers[0]
        assert orphan_info.name == "openscientist-agent-xyz"
        assert orphan_info.job_id == orphan_job_id
        assert orphan_info.container_type == "agent"
        # the real job's container groups, proving the job map came from the database
        assert len(data.job_groups) == 1
        assert data.job_groups[0].job_id == str(test_job.id)
