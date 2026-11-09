"""The master secret's startup gate and the web's error surfaces.

The gate: Settings refuse a missing, placeholder or short OPENSCIENTIST_SECRET_KEY,
and the web exits instead of starting on one. The surfaces: the 500 page carries
no exception text, socket.io accepts only the app's own origin, and the
configuration-error mode signs storage with a per-process random secret.
"""

import asyncio
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from openscientist import settings as settings_module
from openscientist import web_app

PLACEHOLDER = "your-64-char-hex-master-secret-here"
GENERATED = secrets.token_hex(32)
SHORT_KEY = "a" * 63


def _build_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str | None):
    if key is None:
        monkeypatch.delenv("OPENSCIENTIST_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENSCIENTIST_SECRET_KEY", key)
    monkeypatch.chdir(tmp_path)
    settings_module.get_settings.cache_clear()
    return settings_module.Settings()


# --- The settings gate --------------------------------------------------------


def test_settings_reject_the_placeholder_key(monkeypatch, tmp_path: Path):
    with pytest.raises(ValidationError) as excinfo:
        _build_settings(monkeypatch, tmp_path, PLACEHOLDER)
    message = str(excinfo.value)
    assert "OPENSCIENTIST_SECRET_KEY" in message
    assert "openssl rand -hex 32" in message


def test_settings_reject_a_short_key(monkeypatch, tmp_path: Path):
    with pytest.raises(ValidationError) as excinfo:
        _build_settings(monkeypatch, tmp_path, SHORT_KEY)
    assert "OPENSCIENTIST_SECRET_KEY" in str(excinfo.value)


def test_settings_reject_a_missing_key(monkeypatch, tmp_path: Path):
    with pytest.raises(ValidationError) as excinfo:
        _build_settings(monkeypatch, tmp_path, None)
    assert "OPENSCIENTIST_SECRET_KEY" in str(excinfo.value)


def test_settings_accept_a_generated_key_and_derive(monkeypatch, tmp_path: Path):
    settings = _build_settings(monkeypatch, tmp_path, GENERATED)
    assert re.fullmatch(r"[0-9a-f]{64}", settings.auth.storage_secret)
    assert re.fullmatch(r"[0-9a-f]{64}", settings.auth.token_encryption_key)
    assert settings.auth.storage_secret != settings.auth.token_encryption_key


# --- APP_URL follows the base URL ----------------------------------------------


def _app_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, base: str | None, app: str | None
) -> str:
    if base is None:
        monkeypatch.delenv("OPENSCIENTIST_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OPENSCIENTIST_BASE_URL", base)
    if app is None:
        monkeypatch.delenv("APP_URL", raising=False)
    else:
        monkeypatch.setenv("APP_URL", app)
    monkeypatch.chdir(tmp_path)
    settings_module.get_settings.cache_clear()
    return settings_module.Settings().auth.app_url


def test_app_url_falls_back_to_the_base_url(monkeypatch, tmp_path: Path):
    assert _app_url(monkeypatch, tmp_path, "https://prod.example", None) == "https://prod.example"


def test_app_url_defaults_to_the_base_url_default(monkeypatch, tmp_path: Path):
    assert _app_url(monkeypatch, tmp_path, None, None) == "http://localhost:8080"


def test_an_explicit_app_url_wins(monkeypatch, tmp_path: Path):
    assert (
        _app_url(monkeypatch, tmp_path, "https://prod.example", "https://oauth.example")
        == "https://oauth.example"
    )


# --- The web refuses to start on a weak key ------------------------------------


def test_main_refuses_to_start_on_the_placeholder_key(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENSCIENTIST_SECRET_KEY", PLACEHOLDER)
    web_app.get_settings.cache_clear()
    with pytest.raises(SystemExit) as excinfo:
        web_app.main(host="127.0.0.1", port=8080, jobs_dir=tmp_path / "jobs")
    assert excinfo.value.code == 1


def test_main_refuses_to_start_on_a_short_key(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENSCIENTIST_SECRET_KEY", SHORT_KEY)
    web_app.get_settings.cache_clear()
    with pytest.raises(SystemExit) as excinfo:
        web_app.main(host="127.0.0.1", port=8080, jobs_dir=tmp_path / "jobs")
    assert excinfo.value.code == 1


def test_main_refuses_to_start_on_a_missing_key(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPENSCIENTIST_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    web_app.get_settings.cache_clear()
    with pytest.raises(SystemExit) as excinfo:
        web_app.main(host="127.0.0.1", port=8080, jobs_dir=tmp_path / "jobs")
    assert excinfo.value.code == 1


def test_create_app_refuses_to_start_on_a_missing_key(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPENSCIENTIST_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    web_app.get_settings.cache_clear()
    with pytest.raises(SystemExit) as excinfo:
        web_app.create_app(tmp_path / "jobs")
    assert excinfo.value.code == 1


@pytest.mark.usefixtures("web_main_environment")
def test_main_starts_with_a_generated_key(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENSCIENTIST_SECRET_KEY", secrets.token_hex(32))
    web_app.get_settings.cache_clear()
    monkeypatch.setattr(web_app, "_settings_error", None)
    monkeypatch.setattr(web_app, "create_app", lambda jobs_dir=None: FastAPI())
    monkeypatch.setattr(web_app, "_refuse_unsafe_database_login", lambda _url: None)
    uvicorn_calls: list[tuple[tuple[object, ...], dict[object, object]]] = []
    monkeypatch.setattr(
        web_app.uvicorn, "run", lambda *args, **kwargs: uvicorn_calls.append((args, kwargs))
    )
    web_app.main(host="127.0.0.1", port=8080, jobs_dir=tmp_path / "jobs")
    assert uvicorn_calls


# --- The 500 page and the socket.io origin --------------------------------------


def _noop(*_args, **_kwargs) -> None:
    pass


def _skip_page_module_import(monkeypatch: pytest.MonkeyPatch) -> None:
    real = web_app.importlib.import_module
    monkeypatch.setattr(
        web_app.importlib,
        "import_module",
        lambda name, package=None: (
            None if name == "openscientist.webapp_components.pages" else real(name, package)
        ),
    )


def _host_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    monkeypatch.setattr(web_app, "_state", web_app._AppState())
    for name in (
        "_register_openapi_docs",
        "_register_health_endpoint",
        "_register_robots_txt",
        "_register_api_routes",
        "_register_oauth_routes",
        "_register_share_routes",
        "_initialize_job_manager_runtime",
        "_register_nicegui_static_files",
        "_register_pwa_metadata",
    ):
        monkeypatch.setattr(web_app, name, _noop)

    # The real run_with cannot re-add middleware to the shared internal app after its stack is built.
    def _fake_run_with(host, **_kwargs) -> None:
        host.mount("/", web_app.app)
        web_app.app.config.add_run_config(
            reload=False,
            title="OpenScientist",
            viewport="width=device-width",
            favicon=None,
            dark=False,
            language=None,
            binding_refresh_interval=0.1,
            reconnect_timeout=3.0,
            message_history_length=100,
            tailwind=True,
            unocss=None,
            prod_js=True,
            show_welcome_message=True,
            markdown=False,
        )

    monkeypatch.setattr(web_app.ui, "run_with", _fake_run_with)
    _skip_page_module_import(monkeypatch)
    return web_app.create_app(tmp_path / "jobs")


def test_a_forced_500_carries_no_exception_text(monkeypatch, tmp_path: Path, caplog):
    host_app = _host_app(monkeypatch, tmp_path)
    marker = "leaky-exception-text-0a1b2c"

    @web_app.ui.page("/secret-surface-boom")
    def _boom():
        raise RuntimeError(marker)

    # The ASGI stack re-raises after the error response is sent, as uvicorn swallows it.
    transport = httpx.ASGITransport(app=host_app, raise_app_exceptions=False)

    async def _get() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/secret-surface-boom")

    with caplog.at_level("ERROR"):
        response = asyncio.run(_get())
    assert response.status_code == 500
    assert marker not in response.text
    assert marker in caplog.text


def test_socket_io_refuses_a_foreign_origin(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENSCIENTIST_BASE_URL", "https://app.example.com")
    web_app.get_settings.cache_clear()
    host_app = _host_app(monkeypatch, tmp_path)
    transport = httpx.ASGITransport(app=host_app)

    async def _poll(origin: str) -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                "/_nicegui_ws/socket.io/?EIO=4&transport=polling", headers={"Origin": origin}
            )

    foreign = asyncio.run(_poll("https://evil.example"))
    assert foreign.status_code == 400

    own = asyncio.run(_poll("https://app.example.com"))
    assert own.status_code == 200
    assert "sid" in own.text


# --- The configuration-error mode's storage secret -------------------------------


_CONFIG_ERROR_SNIPPET = "import openscientist.web_app as w; print(w.STORAGE_SECRET)"


def _config_error_secret(tmp_path: Path) -> str:
    env = dict(os.environ)
    env["OPENSCIENTIST_SECRET_KEY"] = secrets.token_hex(32)
    env["DATABASE_URL"] = "postgresql+asyncpg://test:test@localhost:5432/test"
    env.pop("ADMIN_DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", _CONFIG_ERROR_SNIPPET],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_config_error_secret_is_random_per_process(tmp_path: Path):
    first = _config_error_secret(tmp_path)
    second = _config_error_secret(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    assert first != second
