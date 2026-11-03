"""Tests for phenix_setup module."""

import os
from unittest.mock import patch

import pytest

from openscientist.phenix_setup import (
    PhenixConfigError,
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
                "OPENSCIENTIST_PROVIDER": "anthropic",
                "OPENSCIENTIST_SECRET_KEY": "a" * 64,
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

    def test_missing_phenix_about_returns_none(self, tmp_path):
        """Install without bin/phenix.about is treated as not-a-Phenix-2.x install."""
        (tmp_path / "bin").mkdir()
        with patch.dict(os.environ, {"PHENIX_PATH": str(tmp_path)}):
            clear_settings_cache()
            assert setup_phenix_env() is None

    def test_missing_phenix_about_raises_with_flag(self, tmp_path):
        (tmp_path / "bin").mkdir()
        with patch.dict(os.environ, {"PHENIX_PATH": str(tmp_path)}):
            clear_settings_cache()
            with pytest.raises(PhenixConfigError) as exc_info:
                setup_phenix_env(raise_on_error=True)
            assert "phenix.about" in str(exc_info.value)

    def test_valid_2x_install_sets_path_and_prefix(self, tmp_path):
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "phenix.about").write_text("phenix 2.x")
        with patch.dict(os.environ, {"PHENIX_PATH": str(tmp_path)}):
            clear_settings_cache()
            env = setup_phenix_env()
            assert env is not None
            assert env["PATH"].startswith(f"{tmp_path}/bin:")
            assert env["PHENIX"] == str(tmp_path)
            assert env["PHENIX_PREFIX"] == str(tmp_path)

    def test_job_id_is_stripped_from_env(self, tmp_path):
        """JOB_ID must not leak into Phenix env (Phenix SGE utils parse it as int)."""
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "phenix.about").write_text("phenix 2.x")
        with patch.dict(os.environ, {"PHENIX_PATH": str(tmp_path), "JOB_ID": "abc-uuid"}):
            clear_settings_cache()
            env = setup_phenix_env()
            assert env is not None
            assert "JOB_ID" not in env

    def test_empty_path_does_not_add_trailing_colon(self, tmp_path):
        """Empty PATH must not yield 'bin_dir:' (trailing colon implies CWD in PATH)."""
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "phenix.about").write_text("phenix 2.x")
        with patch.dict(os.environ, {"PHENIX_PATH": str(tmp_path), "PATH": ""}):
            clear_settings_cache()
            env = setup_phenix_env()
            assert env is not None
            assert env["PATH"] == f"{tmp_path}/bin"
            assert not env["PATH"].endswith(":")
