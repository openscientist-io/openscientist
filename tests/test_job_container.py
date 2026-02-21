"""Tests for shandy.job_container module."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from shandy.job_container.monitor import ContainerMonitor
from shandy.job_container.runner import JobContainerRunner


class TestJobContainerRunner:
    """Tests for JobContainerRunner init and availability."""

    def test_docker_unavailable(self):
        # Create a mock docker module where from_env() raises
        mock_docker = MagicMock()
        mock_docker.from_env.side_effect = Exception("Docker not running")

        with patch.dict(sys.modules, {"docker": mock_docker}):
            runner = JobContainerRunner()
            assert runner.is_available() is False
            assert runner._docker is None

    def test_docker_available(self):
        mock_client = MagicMock()
        mock_docker = MagicMock()
        mock_docker.from_env.return_value = mock_client

        with patch.dict(sys.modules, {"docker": mock_docker}):
            runner = JobContainerRunner()
            assert runner.is_available() is True
            assert runner._docker is mock_client


class TestContainerMonitor:
    """Tests for ContainerMonitor."""

    def test_init_stores_params(self):
        callback = MagicMock()
        monitor = ContainerMonitor(
            job_id="test-job-id",
            on_terminal=callback,
            timeout_hours=2,
        )
        assert monitor._job_id == "test-job-id"
        assert monitor._on_terminal is callback
        assert monitor._timeout_hours == 2

    @pytest.mark.asyncio
    async def test_cancel_before_start_no_error(self):
        callback = MagicMock()
        monitor = ContainerMonitor(
            job_id="test-job-id",
            on_terminal=callback,
        )
        # Cancel before start should not raise
        await monitor.cancel()

    def test_default_timeout(self):
        monitor = ContainerMonitor(
            job_id="j1",
            on_terminal=lambda jid, status: None,
        )
        assert monitor._timeout_hours == 4
