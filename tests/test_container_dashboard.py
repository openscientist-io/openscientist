"""Tests for the container dashboard data service."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import docker
from openscientist.database.models import Job, User
from openscientist.job_container.launcher_client import LauncherUnavailableError
from openscientist.webapp_components.components.common import format_uptime
from openscientist.webapp_components.utils.container_dashboard import (
    _parse_container_stats,
    collect_dashboard_data,
)
from tests.helpers import fake_admin_session
from tests.launcher import (
    EXECUTOR_IMAGE,
    LAUNCHER_INSTANCE_ID,
    Launcher,
    executor_image_problem,
    image_digest,
)

LAUNCHER_PATCH = "openscientist.webapp_components.utils.container_dashboard.LauncherClient"


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
                "cpu_usage": {"total_usage": 100, "percpu_usage": [100]},
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
                "cpu_usage": {"total_usage": 100, "percpu_usage": [100]},
                "system_cpu_usage": 500,
            },
            "memory_stats": {"usage": 0, "stats": {}, "limit": 0},
        }
        cpu, _, _ = _parse_container_stats(stats)
        # (100 / 500) * 4 * 100 = 80%
        assert cpu == pytest.approx(80.0)


def _launcher_entry(
    container_id: str,
    name: str,
    labels: dict,
    status: str = "running",
) -> dict:
    """One entry the launcher's list endpoint returns."""
    return {
        "id": container_id,
        "name": name,
        "status": status,
        "created": "2026-01-15T10:00:00Z",
        "labels": labels,
    }


class TestCollectDashboardLauncherDown:
    @pytest.mark.asyncio
    async def test_returns_unavailable_when_launcher_down(self):
        """With the launcher unreachable the dashboard reports unavailability and no groups."""
        client = MagicMock()
        client.health.side_effect = LauncherUnavailableError(
            "unavailable", "the launcher could not be reached"
        )
        with patch(LAUNCHER_PATCH, return_value=client):
            data = await collect_dashboard_data()

        assert data.docker_available is False
        assert data.error_message is not None
        assert "launcher" in data.error_message.lower()
        assert data.job_groups == []


class TestCollectDashboardGrouping:
    @pytest.mark.asyncio
    async def test_groups_containers_by_job(
        self, db_session: AsyncSession, test_job: Job, test_user: User
    ):
        """Containers with the same job label are grouped with the job's real database metadata."""
        job_id = str(test_job.id)

        agent = _launcher_entry(
            "abc123",
            "openscientist-agent-abc",
            {"openscientist.type": "agent", "openscientist.job_id": job_id},
        )
        executor = _launcher_entry(
            "def456",
            "openscientist-exec-def",
            {"openscientist.type": "executor", "openscientist.job_id": job_id},
        )

        client = MagicMock()
        client.health.return_value = {"docker": True, "images": {"agent": True, "executor": True}}
        client.list_containers.return_value = [agent, executor]

        # _get_active_jobs_map imports get_admin_session at call time, so patch
        # the source module
        with (
            patch(
                "openscientist.database.session.get_admin_session",
                fake_admin_session(db_session),
            ),
            patch(LAUNCHER_PATCH, return_value=client),
        ):
            data = await collect_dashboard_data(include_stats=False)

        client.list_containers.assert_called_once_with(["openscientist.type"])
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

        orphan = _launcher_entry(
            "xyz789",
            "openscientist-agent-xyz",
            {"openscientist.type": "agent", "openscientist.job_id": orphan_job_id},
            status="exited",
        )
        grouped = _launcher_entry(
            "abc123",
            "openscientist-agent-abc",
            {"openscientist.type": "agent", "openscientist.job_id": str(test_job.id)},
        )

        client = MagicMock()
        client.health.return_value = {"docker": True, "images": {"agent": True, "executor": True}}
        client.list_containers.return_value = [orphan, grouped]

        with (
            patch(
                "openscientist.database.session.get_admin_session",
                fake_admin_session(db_session),
            ),
            patch(LAUNCHER_PATCH, return_value=client),
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


@pytest.mark.integration
class TestCollectDashboardLauncherIntegration:
    """The dashboard against the real launcher and Docker daemon."""

    @pytest.fixture(autouse=True)
    def check_docker(self):
        if problem := executor_image_problem():
            pytest.skip(problem)

    @pytest.fixture
    def jobs_root(self, tmp_path) -> Any:
        from pathlib import Path

        jobs = Path(tmp_path) / "jobs"
        (jobs / "11111111-2222-3333-4444-555555555555").mkdir(parents=True)
        return jobs

    @pytest.fixture
    def launcher(self, tmp_path, jobs_root: Any) -> Any:
        client = docker.from_env()
        digest = image_digest(client, EXECUTOR_IMAGE)
        started = Launcher.start(
            tmp_path,
            jobs_root,
            AGENT_IMAGE_DIGESTS=digest,
            EXECUTOR_IMAGE_DIGESTS=digest,
        )
        try:
            yield started
        finally:
            started.close()

    @pytest.mark.asyncio
    async def test_lists_only_launcher_labelled_containers(
        self,
        db_session: AsyncSession,
        test_job: Job,
        launcher: Any,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A container the launcher never created never reaches the dashboard."""
        from openscientist.settings import clear_settings_cache

        client = docker.from_env()
        foreign = client.containers.create(
            EXECUTOR_IMAGE,
            name="openscientist-agent-foreign",
            labels={
                "openscientist.type": "agent",
                "openscientist.job_id": str(test_job.id),
            },
        )
        labelled = client.containers.create(
            EXECUTOR_IMAGE,
            name="openscientist-agent-marked",
            labels={
                "openscientist.type": "agent",
                "openscientist.job_id": str(test_job.id),
                "openscientist.launcher": LAUNCHER_INSTANCE_ID,
            },
        )
        try:
            monkeypatch.setenv("OPENSCIENTIST_LAUNCHER_SOCKET_PATH", str(launcher.socket_path))
            clear_settings_cache()
            with patch(
                "openscientist.database.session.get_admin_session",
                fake_admin_session(db_session),
            ):
                data = await collect_dashboard_data(include_stats=False)
        finally:
            clear_settings_cache()
            foreign.remove(force=True)
            labelled.remove(force=True)

        assert data.docker_available is True
        assert data.error_message is None
        # both containers exist in the daemon; only the launcher-labelled one
        # reaches the dashboard
        assert len(data.job_groups) == 1
        agent = data.job_groups[0].agent_container
        assert agent is not None
        assert agent.name == "openscientist-agent-marked"
        assert data.orphan_containers == []
