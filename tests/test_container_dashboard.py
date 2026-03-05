"""Tests for the container dashboard data service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openscientist.webapp_components.ui_components import format_uptime
from openscientist.webapp_components.utils.container_dashboard import (
    _parse_container_stats,
    collect_dashboard_data,
)

# ---------------------------------------------------------------------------
# format_uptime
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _parse_container_stats
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# collect_dashboard_data — Docker unavailable
# ---------------------------------------------------------------------------


class TestCollectDashboardDockerDown:
    @pytest.mark.asyncio
    async def test_returns_unavailable_when_docker_down(self):
        with patch(
            "openscientist.webapp_components.utils.container_dashboard._get_docker_client",
            return_value=None,
        ):
            data = await collect_dashboard_data()

        assert data.docker_available is False
        assert data.error_message is not None


# ---------------------------------------------------------------------------
# collect_dashboard_data — grouping & orphans
# ---------------------------------------------------------------------------


def _make_mock_container(
    short_id: str,
    name: str,
    labels: dict,
    status: str = "running",
) -> MagicMock:
    c = MagicMock()
    c.short_id = short_id
    c.name = name
    c.labels = labels
    c.status = status
    c.attrs = {"Created": "2026-01-15T10:00:00Z"}
    return c


class TestCollectDashboardGrouping:
    @pytest.mark.asyncio
    async def test_groups_containers_by_job(self):
        job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        mock_settings = MagicMock()

        agent = _make_mock_container(
            "abc123",
            "openscientist-agent-abc",
            {"openscientist.type": "agent", "openscientist.job_id": job_id},
        )
        executor = _make_mock_container(
            "def456",
            "openscientist-exec-def",
            {"openscientist.type": "executor", "openscientist.job_id": job_id},
        )

        mock_client = MagicMock()

        job_map = {
            job_id: {
                "title": "Test job",
                "status": "running",
                "owner_email": "test@example.com",
                "current_iteration": 2,
                "max_iterations": 5,
            },
        }

        with (
            patch(
                "openscientist.settings.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._get_docker_client",
                return_value=mock_client,
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._list_openscientist_containers",
                return_value=[agent, executor],
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._get_active_jobs_map",
                return_value=job_map,
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._get_container_stats",
                return_value={},
            ),
        ):
            data = await collect_dashboard_data(include_stats=False)

        assert len(data.job_groups) == 1
        group = data.job_groups[0]
        assert group.job_id == job_id
        assert group.agent_container is not None
        assert group.agent_container.name == "openscientist-agent-abc"
        assert len(group.executor_containers) == 1
        assert data.orphan_containers == []

    @pytest.mark.asyncio
    async def test_orphan_containers_detected(self):
        """Container with a job_id not in the database should be flagged as orphan."""
        orphan_job_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"

        mock_settings = MagicMock()

        container = _make_mock_container(
            "xyz789",
            "openscientist-agent-xyz",
            {"openscientist.type": "agent", "openscientist.job_id": orphan_job_id},
            status="exited",
        )

        mock_client = MagicMock()

        with (
            patch(
                "openscientist.settings.get_settings",
                return_value=mock_settings,
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._get_docker_client",
                return_value=mock_client,
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._list_openscientist_containers",
                return_value=[container],
            ),
            patch(
                "openscientist.webapp_components.utils.container_dashboard._get_active_jobs_map",
                return_value={},  # empty — job doesn't exist in DB
            ),
        ):
            data = await collect_dashboard_data(include_stats=False)

        assert len(data.job_groups) == 0
        assert len(data.orphan_containers) == 1
        assert data.orphan_containers[0].name == "openscientist-agent-xyz"
