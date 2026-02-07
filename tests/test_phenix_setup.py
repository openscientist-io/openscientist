"""Tests for phenix_setup module."""

import os
from unittest.mock import MagicMock, patch

from shandy.phenix_setup import check_phenix_available, setup_phenix_env


class TestSetupPhenixEnv:
    """Tests for Phenix environment setup."""

    @patch.dict(os.environ, {}, clear=True)
    def test_no_phenix_path_returns_none(self):
        result = setup_phenix_env()
        assert result is None

    @patch.dict(os.environ, {"PHENIX_PATH": "/nonexistent/path"})
    def test_nonexistent_path_returns_none(self):
        result = setup_phenix_env()
        assert result is None

    @patch("shandy.phenix_setup.subprocess.run")
    @patch("shandy.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_successful_setup(self, mock_exists, mock_run):
        mock_exists.return_value = True
        mock_run.return_value = MagicMock(
            stdout="PATH=/opt/phenix/bin:/usr/bin\nPHENIX_HOME=/opt/phenix\n"
        )
        result = setup_phenix_env()
        assert result is not None
        assert "PHENIX_HOME" in result

    @patch("shandy.phenix_setup.subprocess.run")
    @patch("shandy.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_timeout_returns_none(self, mock_exists, mock_run):
        import subprocess

        mock_exists.return_value = True
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="source", timeout=10)
        result = setup_phenix_env()
        assert result is None

    @patch("shandy.phenix_setup.subprocess.run")
    @patch("shandy.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_generic_exception_returns_none(self, mock_exists, mock_run):
        mock_exists.return_value = True
        mock_run.side_effect = OSError("exec failed")
        result = setup_phenix_env()
        assert result is None


class TestCheckPhenixAvailable:
    """Tests for Phenix availability check."""

    @patch("shandy.phenix_setup.setup_phenix_env")
    def test_available_when_setup_returns_env(self, mock_setup):
        mock_setup.return_value = {"PATH": "/usr/bin"}
        assert check_phenix_available() is True

    @patch("shandy.phenix_setup.setup_phenix_env")
    def test_unavailable_when_setup_returns_none(self, mock_setup):
        mock_setup.return_value = None
        assert check_phenix_available() is False
