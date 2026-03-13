from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from openscientist import web_app


def _noop(*_args, **_kwargs) -> None:
    pass


def test_create_app_builds_host_app_once(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_app, "_state", web_app._AppState())
    monkeypatch.setattr(web_app, "_register_openapi_docs", _noop)
    monkeypatch.setattr(web_app, "_register_health_endpoint", _noop)
    monkeypatch.setattr(web_app, "_register_robots_txt", _noop)
    monkeypatch.setattr(web_app, "_register_api_routes", _noop)
    monkeypatch.setattr(web_app, "_register_oauth_routes", _noop)
    monkeypatch.setattr(web_app, "_register_share_routes", _noop)
    monkeypatch.setattr(web_app, "_initialize_job_manager_runtime", _noop)
    monkeypatch.setattr(web_app, "_register_nicegui_static_files", _noop)
    monkeypatch.setattr(web_app, "_register_pwa_metadata", _noop)
    monkeypatch.setattr(web_app.importlib, "import_module", lambda _name: None)

    run_with_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        web_app.ui,
        "run_with",
        lambda *args, **kwargs: run_with_calls.append((args, kwargs)),
    )

    app_one = web_app.create_app(tmp_path / "jobs")
    app_two = web_app.create_app(tmp_path / "other-jobs")

    assert isinstance(app_one, FastAPI)
    assert app_one is app_two
    assert run_with_calls
    assert run_with_calls[0][0][0] is app_one
    assert run_with_calls[0][1]["mount_path"] == "/"


def test_main_reload_uses_factory_import_target(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_app, "_settings_error", None)
    monkeypatch.setattr(
        "openscientist.settings.get_settings",
        lambda: SimpleNamespace(dev=SimpleNamespace(dev_mode=True)),
    )

    uvicorn_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        web_app.uvicorn,
        "run",
        lambda *args, **kwargs: uvicorn_calls.append((args, kwargs)),
    )

    web_app.main(host="127.0.0.1", port=9999, jobs_dir=tmp_path / "jobs")

    assert uvicorn_calls
    args, kwargs = uvicorn_calls[0]
    assert args[0] == "openscientist.web_app:create_app"
    assert kwargs["factory"] is True
    assert kwargs["reload"] is True
    assert Path(web_app.os.environ[web_app.JOBS_DIR_ENV]) == tmp_path / "jobs"


def test_main_non_reload_runs_with_created_app(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_app, "_settings_error", None)
    monkeypatch.setattr(
        "openscientist.settings.get_settings",
        lambda: SimpleNamespace(dev=SimpleNamespace(dev_mode=False)),
    )

    host_app = FastAPI()
    monkeypatch.setattr(web_app, "create_app", lambda jobs_dir=None: host_app)

    uvicorn_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(
        web_app.uvicorn,
        "run",
        lambda *args, **kwargs: uvicorn_calls.append((args, kwargs)),
    )

    web_app.main(host="127.0.0.1", port=9999, jobs_dir=tmp_path / "jobs")

    assert uvicorn_calls
    args, kwargs = uvicorn_calls[0]
    assert args[0] is host_app
    assert kwargs["reload"] is False


def test_register_nicegui_static_files_tolerates_duplicates(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def fake_add_static_files(route: str, directory: str) -> None:
        calls.append((route, directory))
        if route == "/assets":
            raise ValueError("already registered")

    monkeypatch.setattr(web_app.app, "add_static_files", fake_add_static_files)

    web_app._register_nicegui_static_files(tmp_path / "jobs")

    routes = [route for route, _ in calls]
    assert routes == ["/jobs", "/assets"]


def test_register_robots_txt_serves_disallow_all() -> None:
    host_app = FastAPI()

    web_app._register_robots_txt(host_app)

    route = next(
        route
        for route in host_app.routes
        if isinstance(route, APIRoute) and route.path == "/robots.txt"
    )
    response = route.endpoint()

    assert response.media_type == "text/plain"
    assert response.body == b"User-agent: *\nDisallow: /\n"


def test_register_apple_touch_icon_redirects_root_requests() -> None:
    host_app = FastAPI()

    web_app._register_apple_touch_icon_redirect(host_app)
    client = TestClient(host_app)

    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        response = client.get(path, follow_redirects=False)

        assert response.status_code == 301
        assert response.headers["location"] == "/assets/apple-touch-icon.png"


def test_register_pwa_metadata_adds_shared_head_html(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        web_app.ui,
        "add_head_html",
        lambda html, shared=False: calls.append((html, shared)),
    )

    web_app._register_pwa_metadata()

    assert calls == [
        (
            "<!-- PWA & iOS Web App -->\n"
            '<meta name="apple-mobile-web-app-capable" content="yes">\n'
            '<meta name="apple-mobile-web-app-status-bar-style" content="default">\n'
            '<meta name="apple-mobile-web-app-title" content="OpenScientist">\n'
            '<meta name="mobile-web-app-capable" content="yes">\n'
            '<meta name="theme-color" content="#0891b2">\n'
            '<meta name="theme-color" content="#0c4a6e" media="(prefers-color-scheme: dark)">\n'
            '<link rel="apple-touch-icon" sizes="180x180" href="/assets/apple-touch-icon.png">\n'
            '<link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon-32.png">\n'
            '<link rel="icon" type="image/png" sizes="16x16" href="/assets/favicon-16.png">\n'
            '<link rel="manifest" href="/assets/manifest.json">',
            True,
        )
    ]
