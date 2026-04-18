"""Tests for phenix_setup module."""

import os
from unittest.mock import patch

import pytest

from openscientist.phenix_setup import (
    PhenixConfigError,
    check_phenix_available,
    setup_phenix_env,
    validate_phenix_path,
)
from openscientist.settings import clear_settings_cache


class TestValidatePhenixPath:
    """Tests for PHENIX_PATH validation."""

    def test_absolute_path_valid(self):
        """Absolute path passes basic validation (existence checked separately)."""
        with (
            patch("openscientist.phenix_setup.os.path.exists", return_value=True),
            patch(
                "openscientist.phenix_setup.os.path.isdir",
                return_value=True,
            ),
        ):
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
        with (
            patch("openscientist.phenix_setup.os.path.exists", return_value=True),
            patch(
                "openscientist.phenix_setup.os.path.isdir",
                return_value=True,
            ),
        ):
            errors = validate_phenix_path("/opt/../etc/phenix")
            assert len(errors) == 1
            assert "path traversal" in errors[0]

    def test_nonexistent_path_rejected(self):
        """Non-existent paths must be rejected with helpful message."""
        errors = validate_phenix_path("/nonexistent/phenix/path")
        assert len(errors) == 1
        assert "does not exist" in errors[0]
        assert "Docker" in errors[0]

    def test_file_instead_of_directory_rejected(self):
        """Files (not directories) must be rejected."""
        with (
            patch("openscientist.phenix_setup.os.path.exists", return_value=True),
            patch(
                "openscientist.phenix_setup.os.path.isdir",
                return_value=False,
            ),
        ):
            errors = validate_phenix_path("/opt/phenix.tar.gz")
            assert len(errors) == 1
            assert "directory" in errors[0]


class TestSetupPhenixEnv:
    """Tests for Phenix 2.x environment setup."""

    def test_no_phenix_path_returns_none(self):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_SECRET_KEY": "test",
                "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
            },
            clear=True,
        ):
            clear_settings_cache()
            assert setup_phenix_env() is None

    def test_nonexistent_path_returns_none(self):
        with patch.dict(os.environ, {"PHENIX_PATH": "/nonexistent/path"}):
            clear_settings_cache()
            assert setup_phenix_env() is None

    def test_relative_path_rejected_by_settings(self):
        """Relative paths are rejected by pydantic settings validation."""
        from pydantic import ValidationError as PydanticValidationError

        with patch.dict(os.environ, {"PHENIX_PATH": "relative/path"}):
            clear_settings_cache()
            with pytest.raises(PydanticValidationError, match="absolute path"):
                from openscientist.settings import get_settings

                get_settings()

    def test_nonexistent_path_raises_with_flag(self):
        with patch.dict(os.environ, {"PHENIX_PATH": "/nonexistent/path"}):
            clear_settings_cache()
            with pytest.raises(PhenixConfigError) as exc_info:
                setup_phenix_env(raise_on_error=True)
            assert "does not exist" in str(exc_info.value)

    @patch("openscientist.phenix_setup.os.path.isdir")
    @patch("openscientist.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_missing_phenix_about_returns_none(self, mock_exists, mock_isdir):
        """Install without bin/phenix.about is treated as not-a-Phenix-2.x install."""

        def exists_side_effect(path):
            return "phenix.about" not in path

        mock_exists.side_effect = exists_side_effect
        mock_isdir.return_value = True
        assert setup_phenix_env() is None

    @patch("openscientist.phenix_setup.os.path.isdir")
    @patch("openscientist.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_missing_phenix_about_raises_with_flag(self, mock_exists, mock_isdir):
        def exists_side_effect(path):
            return "phenix.about" not in path

        mock_exists.side_effect = exists_side_effect
        mock_isdir.return_value = True
        with pytest.raises(PhenixConfigError) as exc_info:
            setup_phenix_env(raise_on_error=True)
        assert "phenix.about" in str(exc_info.value)

    @patch("openscientist.phenix_setup.os.path.isdir")
    @patch("openscientist.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix"})
    def test_valid_2x_install_sets_path_and_prefix(self, mock_exists, mock_isdir):
        clear_settings_cache()
        mock_exists.return_value = True
        mock_isdir.return_value = True
        env = setup_phenix_env()
        assert env is not None
        assert env["PATH"].startswith("/opt/phenix/bin:")
        assert env["PHENIX"] == "/opt/phenix"
        assert env["PHENIX_PREFIX"] == "/opt/phenix"

    @patch("openscientist.phenix_setup.os.path.isdir")
    @patch("openscientist.phenix_setup.os.path.exists")
    @patch.dict(os.environ, {"PHENIX_PATH": "/opt/phenix", "PATH": ""})
    def test_empty_path_does_not_add_trailing_colon(self, mock_exists, mock_isdir):
        """Empty PATH must not yield 'bin_dir:' (trailing colon implies CWD in PATH)."""
        clear_settings_cache()
        mock_exists.return_value = True
        mock_isdir.return_value = True
        env = setup_phenix_env()
        assert env is not None
        assert env["PATH"] == "/opt/phenix/bin"
        assert not env["PATH"].endswith(":")


class TestCheckPhenixAvailable:
    """Tests for Phenix availability check."""

    def test_available_when_phenix_configured(self, monkeypatch, tmp_path):
        """check_phenix_available returns True when a 2.x install is present."""
        phenix_dir = tmp_path / "phenix"
        phenix_dir.mkdir()
        (phenix_dir / "bin").mkdir()
        (phenix_dir / "bin" / "phenix.about").write_text("#!/bin/sh\n")

        clear_settings_cache()
        monkeypatch.chdir(tmp_path)  # Avoid picking up .env
        monkeypatch.setenv("PHENIX_PATH", str(phenix_dir))

        assert check_phenix_available() is True
        clear_settings_cache()

    def test_unavailable_when_phenix_not_configured(self, monkeypatch, tmp_path):
        clear_settings_cache()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PHENIX_PATH", raising=False)

        assert check_phenix_available() is False
        clear_settings_cache()

    @patch("openscientist.phenix_setup.setup_phenix_env")
    def test_fallback_to_setup_phenix_env(self, mock_setup):
        """Falls back to setup_phenix_env when settings can't be loaded."""
        with patch("openscientist.settings.get_settings", side_effect=Exception("Settings error")):
            mock_setup.return_value = {"PATH": "/usr/bin"}
            assert check_phenix_available() is True

            mock_setup.return_value = None
            assert check_phenix_available() is False
