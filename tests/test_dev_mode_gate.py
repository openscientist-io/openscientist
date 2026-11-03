"""Dev mode allows anonymous logins, so the web refuses it on a public address."""

from pathlib import Path

import pytest

from openscientist import web_app
from openscientist.settings import Settings, dev_mode_problems


def _settings(
    monkeypatch: pytest.MonkeyPatch, dev_mode: bool, app_url: str, bind_address: str
) -> Settings:
    monkeypatch.setenv("OPENSCIENTIST_DEV_MODE", "true" if dev_mode else "false")
    monkeypatch.setenv("APP_URL", app_url)
    monkeypatch.setenv("OPENSCIENTIST_BIND_ADDRESS", bind_address)
    return Settings(OPENSCIENTIST_SECRET_KEY="a" * 64)  # type: ignore[call-arg]


def test_dev_mode_is_safe_with_a_local_url_and_loopback_bind(monkeypatch):
    settings = _settings(monkeypatch, True, "http://localhost:8080", "127.0.0.1")
    assert dev_mode_problems(settings) == []


@pytest.mark.parametrize("app_url", ["http://127.0.0.1:8080", "http://[::1]:8080"])
def test_dev_mode_is_safe_with_a_loopback_host(app_url: str, monkeypatch):
    settings = _settings(monkeypatch, True, app_url, "127.0.0.1")
    assert dev_mode_problems(settings) == []


def test_dev_mode_is_safe_with_an_ipv6_loopback_bind(monkeypatch):
    settings = _settings(monkeypatch, True, "http://localhost:8080", "::1")
    assert dev_mode_problems(settings) == []


def test_dev_mode_is_refused_for_a_public_app_url(monkeypatch):
    settings = _settings(monkeypatch, True, "https://sci.example.com", "127.0.0.1")
    assert dev_mode_problems(settings)


def test_dev_mode_is_refused_for_an_https_local_url(monkeypatch):
    settings = _settings(monkeypatch, True, "https://localhost:8080", "127.0.0.1")
    assert dev_mode_problems(settings)


def test_dev_mode_is_refused_for_a_non_loopback_bind(monkeypatch):
    settings = _settings(monkeypatch, True, "http://localhost:8080", "0.0.0.0")
    assert dev_mode_problems(settings)


def test_dev_mode_is_refused_for_a_public_bind(monkeypatch):
    settings = _settings(monkeypatch, True, "http://localhost:8080", "203.0.113.10")
    assert dev_mode_problems(settings)


def test_an_empty_bind_address_falls_back_to_the_loopback_default(monkeypatch):
    monkeypatch.setenv("OPENSCIENTIST_BIND_ADDRESS", "")
    settings = Settings(OPENSCIENTIST_SECRET_KEY="a" * 64)  # type: ignore[call-arg]
    assert settings.bind_address == "127.0.0.1"


def test_production_mode_needs_no_local_url_or_loopback_bind(monkeypatch):
    settings = _settings(monkeypatch, False, "https://sci.example.com", "0.0.0.0")
    assert dev_mode_problems(settings) == []


def _run_main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dev_mode: bool, app_url: str, bind_address: str
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    settings = _settings(monkeypatch, dev_mode, app_url, bind_address)
    monkeypatch.setattr(web_app, "_settings_error", None)
    monkeypatch.setattr("openscientist.settings.get_settings", lambda: settings)
    monkeypatch.setattr(web_app, "_refuse_unsafe_database_login", lambda _url: None)
    if not dev_mode:
        from fastapi import FastAPI

        monkeypatch.setattr(web_app, "create_app", lambda jobs_dir=None: FastAPI())
    uvicorn_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        web_app.uvicorn, "run", lambda *args, **kwargs: uvicorn_calls.append((args, kwargs))
    )
    return uvicorn_calls


def test_main_refuses_dev_mode_with_a_public_app_url(monkeypatch, tmp_path: Path):
    uvicorn_calls = _run_main(monkeypatch, tmp_path, True, "https://sci.example.com", "127.0.0.1")
    with pytest.raises(SystemExit):
        web_app.main(host="127.0.0.1", port=9999, jobs_dir=tmp_path / "jobs")
    assert uvicorn_calls == []


def test_main_refuses_dev_mode_with_a_non_loopback_bind(monkeypatch, tmp_path: Path):
    uvicorn_calls = _run_main(monkeypatch, tmp_path, True, "http://localhost:8080", "0.0.0.0")
    with pytest.raises(SystemExit):
        web_app.main(host="127.0.0.1", port=9999, jobs_dir=tmp_path / "jobs")
    assert uvicorn_calls == []


def test_main_starts_dev_mode_with_a_local_url_and_loopback_bind(monkeypatch, tmp_path: Path):
    uvicorn_calls = _run_main(monkeypatch, tmp_path, True, "http://localhost:8080", "127.0.0.1")
    web_app.main(host="127.0.0.1", port=9999, jobs_dir=tmp_path / "jobs")
    assert uvicorn_calls
    args, kwargs = uvicorn_calls[0]
    assert args[0] == "openscientist.web_app:create_app"
    assert kwargs["reload"] is True


def test_main_starts_production_mode_with_a_public_bind(monkeypatch, tmp_path: Path):
    uvicorn_calls = _run_main(monkeypatch, tmp_path, False, "https://sci.example.com", "0.0.0.0")
    web_app.main(host="127.0.0.1", port=9999, jobs_dir=tmp_path / "jobs")
    assert uvicorn_calls
    assert uvicorn_calls[0][1]["reload"] is False
