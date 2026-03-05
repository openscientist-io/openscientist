"""Tests for open_scientist.version module."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import open_scientist.version as version_mod


class TestGetCommit:
    """Tests for get_commit()."""

    def setup_method(self):
        """Reset cached values before each test."""
        version_mod._commit = None

    def teardown_method(self):
        version_mod._commit = None

    @patch.dict(os.environ, {"OPEN_SCIENTIST_COMMIT": "abc123def456789"})
    def test_from_env_returns_first_12_chars(self):
        result = version_mod.get_commit()
        assert result == "abc123def456"

    @patch.dict(os.environ, {"OPEN_SCIENTIST_COMMIT": "unknown"})
    @patch("open_scientist.version.subprocess.run")
    def test_ignores_unknown_and_falls_through_to_git(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="fedcba987654321\n")
        result = version_mod.get_commit()
        assert result == "fedcba987654"
        mock_run.assert_called_once()

    @patch.dict(os.environ, {}, clear=True)
    @patch("open_scientist.version.subprocess.run")
    def test_from_git(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="0123456789abcdef\n")
        result = version_mod.get_commit()
        assert result == "0123456789ab"

    @patch.dict(os.environ, {}, clear=True)
    @patch("open_scientist.version.subprocess.run", side_effect=OSError("git not found"))
    def test_git_fails_returns_unknown(self, _mock_run):
        result = version_mod.get_commit()
        assert result == "unknown"

    @patch.dict(os.environ, {}, clear=True)
    @patch(
        "open_scientist.version.subprocess.run",
        side_effect=subprocess.SubprocessError("timeout"),
    )
    def test_git_subprocess_error_returns_unknown(self, _mock_run):
        result = version_mod.get_commit()
        assert result == "unknown"

    @patch.dict(os.environ, {"OPEN_SCIENTIST_COMMIT": "aabbccddee11"})
    def test_caches_result(self):
        first = version_mod.get_commit()
        # Change env — should NOT affect result because it's cached
        with patch.dict(os.environ, {"OPEN_SCIENTIST_COMMIT": "different_hash"}):
            second = version_mod.get_commit()
        assert first == second == "aabbccddee11"

    @patch.dict(os.environ, {}, clear=True)
    @patch("open_scientist.version.subprocess.run")
    def test_git_nonzero_returncode_returns_unknown(self, mock_run):
        mock_run.return_value = MagicMock(returncode=128, stdout="")
        result = version_mod.get_commit()
        assert result == "unknown"


class TestGetBuildTime:
    """Tests for get_build_time()."""

    def setup_method(self):
        version_mod._build_time = None

    def teardown_method(self):
        version_mod._build_time = None

    @patch.dict(os.environ, {"OPEN_SCIENTIST_BUILD_TIME": "2026-02-01T12:00:00"})
    def test_from_env(self):
        result = version_mod.get_build_time()
        assert result == "2026-02-01T12:00:00"

    @patch.dict(os.environ, {}, clear=True)
    def test_no_env_returns_dev(self):
        result = version_mod.get_build_time()
        assert result == "dev"

    @patch.dict(os.environ, {"OPEN_SCIENTIST_BUILD_TIME": "unknown"})
    def test_unknown_env_returns_dev(self):
        result = version_mod.get_build_time()
        assert result == "dev"

    @patch.dict(os.environ, {"OPEN_SCIENTIST_BUILD_TIME": ""})
    def test_empty_env_returns_dev(self):
        result = version_mod.get_build_time()
        assert result == "dev"


class TestGetVersionString:
    """Tests for get_version_string()."""

    def setup_method(self):
        version_mod._commit = None
        version_mod._build_time = None

    def teardown_method(self):
        version_mod._commit = None
        version_mod._build_time = None

    @patch.dict(
        os.environ,
        {"OPEN_SCIENTIST_COMMIT": "abc123def456", "OPEN_SCIENTIST_BUILD_TIME": "2026-01-01"},
    )
    def test_combines_version_commit_build_time(self):
        result = version_mod.get_version_string()
        assert (
            result
            == f"OpenScientist v{version_mod.__version__} (commit: abc123def456, built: 2026-01-01)"
        )

    @patch.dict(os.environ, {}, clear=True)
    @patch("open_scientist.version.subprocess.run", side_effect=OSError)
    def test_fallback_values(self, _mock_run):
        result = version_mod.get_version_string()
        assert "unknown" in result
        assert "dev" in result
