"""Tests for open_scientist.job_container module."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from open_scientist.job_container.runner import JobContainerRunner


class TestJobContainerRunner:
    """Tests for JobContainerRunner init."""

    def test_docker_unavailable_raises(self):
        mock_docker = MagicMock()
        mock_docker.from_env.side_effect = Exception("Docker not running")

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with pytest.raises(Exception, match="Docker not running"):
                JobContainerRunner()

    def test_docker_available(self):
        mock_client = MagicMock()
        mock_docker = MagicMock()
        mock_docker.from_env.return_value = mock_client

        with patch.dict(sys.modules, {"docker": mock_docker}):
            runner = JobContainerRunner()
            assert runner._docker is mock_client
