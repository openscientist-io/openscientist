"""Tests for phenix_setup module."""

import os
from unittest.mock import MagicMock, patch

import pytest

from shandy.phenix_setup import (
    PhenixConfigError,
    check_phenix_available,
    setup_phenix_env,
    validate_phenix_path,
)


class TestValidatePhenixPath:
    """Tests for PHENIX_PATH validation."""

    def test_absolute_path_valid(self):
        """Absolute path passes basic validation (existence checked separately)."""
        with patch("shandy.phenix_setup.os.path.exists", return_value=True):
            with patch("shandy.phenix_setup.os.path.isdir", return_value=True):
                errors = validate_phenix_path("/opt/phenix")
                assert errors == []

    def test_relative_path_rejected(self):
        """Relative paths must be rejected."""
        errors = validate_phenix_path("phenix")
        assert len(errors) == 1
        assert "absolute path" in errors[0]
        assert "starting with '/'" in errors[0]

    def test_relative_path_with_dot_rejected(self):
        """Paths starting with ./ must be rejected."""
        errors = validate_phenix_path("./phenix")
        assert len(errors) == 1
        assert "absolute path" in errors[0]

    def test_path_traversal_rejected(self):
        """Paths with .. must be rejected."""
        with patch("shandy.phenix_setup.os.path.exists", return_value=True):
            with patch("shandy.phenix_setup.os.path.isdir", return_value=True):
                errors = validate_phenix_path("/opt/../etc/phenix")
                assert len(errors) == 1
                assert "path traversal" in errors[0]

    def test_nonexistent_path_rejected(self):
        """Non-existent paths must be rejected with helpful message."""
        errors = validate_phenix_path("/nonexistent/phenix/path")
        assert len(errors) == 1
        assert "does not exist" in errors[0]
        assert "Docker" in errors[0]  # Should mention Docker context

    def test_file_instead_of_directory_rejected(self):
        """Files (not directories) must be rejected."""
        with patch("shandy.phenix_setup.os.path.exists", return_value=True):
            with patch("shandy.phenix_setup.os.path.isdir", return_value=False):
                errors = validate_phenix_path("/opt/phenix.tar.gz")
                assert len(errors) == 1
                assert "directory" in errors[0]


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

    @patch.dict(os.environ, {"PHENIX_PATH": "relative/path"})
    def test_relative_path_returns_none(self):
        """Relative paths should return None (invalid config)."""
        result = setup_phenix_env()
        assert result is None

    @patch.dict(os.environ, {"PHENIX_PATH": "relative/path"})
    def test_relative_path_raises_with_flag(self):
        """Relative paths should raise when raise_on_error=True."""
        with pytest.raises(PhenixConfigError) as exc_info:
            setup_phenix_env(raise_on_error=True)
        assert "absolute path" in str(exc_info.value)

    @patch.dict(os.environ, {"PHENIX_PATH": "/nonexistent/path"})
    def test_nonexistent_path_raises_with_flag(self):
        """Non-existent paths should raise when raise_on_error=True."""
        with pytest.raises(PhenixConfigError) as exc_info:
            setup_phenix_env(raise_on_error=True)
        assert "does not exist" in str(exc_info.value)

    @patch("shandy.phenix_setup.subprocess.run")
    @patch("shandy.phenix_setup.os.path.isdir")
    @patch("shandy.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_successful_setup(self, mock_exists, mock_isdir, mock_run):
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_run.return_value = MagicMock(
            stdout="PATH=/opt/phenix/bin:/usr/bin\nPHENIX_HOME=/opt/phenix\n"
        )
        result = setup_phenix_env()
        assert result is not None
        assert "PHENIX_HOME" in result

    @patch("shandy.phenix_setup.subprocess.run")
    @patch("shandy.phenix_setup.os.path.isdir")
    @patch("shandy.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_timeout_returns_none(self, mock_exists, mock_isdir, mock_run):
        import subprocess

        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="source", timeout=10)
        result = setup_phenix_env()
        assert result is None

    @patch("shandy.phenix_setup.subprocess.run")
    @patch("shandy.phenix_setup.os.path.isdir")
    @patch("shandy.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_generic_exception_returns_none(self, mock_exists, mock_isdir, mock_run):
        mock_exists.return_value = True
        mock_isdir.return_value = True
        mock_run.side_effect = OSError("exec failed")
        result = setup_phenix_env()
        assert result is None

    @patch("shandy.phenix_setup.os.path.isdir")
    @patch("shandy.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_missing_env_script_returns_none(self, mock_exists, mock_isdir):
        """Missing phenix_env.sh should return None."""

        # Directory exists but phenix_env.sh does not
        def exists_side_effect(path):
            return not path.endswith("phenix_env.sh")

        mock_exists.side_effect = exists_side_effect
        mock_isdir.return_value = True
        result = setup_phenix_env()
        assert result is None

    @patch("shandy.phenix_setup.os.path.isdir")
    @patch("shandy.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_missing_env_script_raises_with_flag(self, mock_exists, mock_isdir):
        """Missing phenix_env.sh should raise when raise_on_error=True."""

        def exists_side_effect(path):
            return not path.endswith("phenix_env.sh")

        mock_exists.side_effect = exists_side_effect
        mock_isdir.return_value = True
        with pytest.raises(PhenixConfigError) as exc_info:
            setup_phenix_env(raise_on_error=True)
        assert "phenix_env.sh" in str(exc_info.value)


class TestCheckPhenixAvailable:
    """Tests for Phenix availability check."""

    def test_available_when_phenix_configured(self, monkeypatch, tmp_path):
        """check_phenix_available returns True when Phenix is properly configured."""
        # Create a fake phenix directory with phenix_env.sh
        phenix_dir = tmp_path / "phenix"
        phenix_dir.mkdir()
        (phenix_dir / "phenix_env.sh").write_text("# phenix env")

        # Clear settings cache and set PHENIX_PATH
        from shandy.settings import clear_settings_cache

        clear_settings_cache()
        monkeypatch.chdir(tmp_path)  # Avoid picking up .env
        monkeypatch.setenv("PHENIX_PATH", str(phenix_dir))
        # Set minimal provider config to avoid validation errors
        monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        assert check_phenix_available() is True

        clear_settings_cache()

    def test_unavailable_when_phenix_not_configured(self, monkeypatch, tmp_path):
        """check_phenix_available returns False when Phenix is not configured."""
        from shandy.settings import clear_settings_cache

        clear_settings_cache()
        monkeypatch.chdir(tmp_path)  # Avoid picking up .env
        monkeypatch.delenv("PHENIX_PATH", raising=False)
        # Set minimal provider config to avoid validation errors
        monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        assert check_phenix_available() is False

        clear_settings_cache()

    @patch("shandy.phenix_setup.setup_phenix_env")
    def test_fallback_to_setup_phenix_env(self, mock_setup):
        """Falls back to setup_phenix_env when settings can't be loaded."""
        # Force settings to fail by making get_settings raise
        with patch("shandy.settings.get_settings", side_effect=Exception("Settings error")):
            mock_setup.return_value = {"PATH": "/usr/bin"}
            assert check_phenix_available() is True

            mock_setup.return_value = None
            assert check_phenix_available() is False
