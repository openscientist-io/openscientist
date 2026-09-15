"""The startup posture log.

These cover the failure this log exists to surface: prod ran with dev mode on,
serving mock auth routes on a public address, because the guard that rejects
that combination reads OPENSCIENTIST_ENVIRONMENT while the deployed .env set a
bare ENVIRONMENT. Nothing in the logs said so for seven weeks.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from openscientist.settings import Settings
from openscientist.web_app import _log_startup_posture

LOGGER = "openscientist.web_app"


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> Settings:
    """A real Settings built from env, so this test tracks the actual fields."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSCIENTIST_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app:pass@host:5432/db")
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql+asyncpg://adm:pass@host:5432/db")
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
    for key in ("OPENSCIENTIST_DEV_MODE", "OPENSCIENTIST_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings()
    monkeypatch.setattr("openscientist.settings.get_settings", lambda: settings)
    return settings


def _capture(caplog: pytest.LogCaptureFixture, env: dict[str, str]) -> str:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _log_startup_posture(env)
    return "\n".join(record.getMessage() for record in caplog.records if record.name == LOGGER)


class TestStartupPosture:
    def test_reports_resolved_environment_and_dev_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The log states what was resolved, not what was configured."""
        _settings(monkeypatch, tmp_path, OPENSCIENTIST_ENVIRONMENT="production")
        text = _capture(caplog, {"OPENSCIENTIST_ENVIRONMENT": "production"})

        assert "Environment:   production" in text
        assert "Dev mode:      disabled" in text
        assert "mock auth routes return 404" in text
        assert "OAuth:         not configured" in text

    def test_warns_when_only_the_unread_environment_variable_is_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The exact mismatch that disabled the production guard for seven weeks."""
        _settings(monkeypatch, tmp_path, OPENSCIENTIST_DEV_MODE="true")
        text = _capture(caplog, {"ENVIRONMENT": "production"})

        assert "OPENSCIENTIST_ENVIRONMENT is not" in text
        assert "resolved to development" in text
        # and it must say the combination is dangerous, not merely odd
        assert "/auth/mock/admin-login" in text

    def test_no_warning_when_the_read_variable_is_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A correctly configured deployment logs its posture and nothing else."""
        _settings(monkeypatch, tmp_path, OPENSCIENTIST_ENVIRONMENT="development")
        _capture(caplog, {"OPENSCIENTIST_ENVIRONMENT": "development"})

        warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING]
        assert warnings == []

    def test_dev_mode_in_development_is_not_flagged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dev mode is the point of a dev box; only flag it elsewhere."""
        _settings(
            monkeypatch,
            tmp_path,
            OPENSCIENTIST_DEV_MODE="true",
            OPENSCIENTIST_ENVIRONMENT="development",
        )
        _capture(caplog, {"OPENSCIENTIST_ENVIRONMENT": "development"})

        warnings = [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING]
        assert warnings == []
