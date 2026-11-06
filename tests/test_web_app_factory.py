from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from uvicorn.supervisors.watchfilesreload import FileFilter

from openscientist import web_app
from openscientist.security import ScannerBlockMiddleware, SecurityHeadersMiddleware


def _noop(*_args, **_kwargs) -> None:
    pass


def _skip_page_module_import(monkeypatch) -> None:
    # nicegui.ui imports its attributes lazily, so only web_app's own call may be neutralised.
    real = web_app.importlib.import_module
    monkeypatch.setattr(
        web_app.importlib,
        "import_module",
        lambda name, package=None: (
            None if name == "openscientist.webapp_components.pages" else real(name, package)
        ),
    )


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
    _skip_page_module_import(monkeypatch)

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
        lambda: SimpleNamespace(
            dev=SimpleNamespace(dev_mode=True),
            database=SimpleNamespace(database_url="unused"),
            auth=SimpleNamespace(app_url="http://localhost:8080"),
            bind_address="127.0.0.1",
        ),
    )
    monkeypatch.setattr(web_app, "_refuse_unsafe_database_login", lambda _url: None)

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


def _restarts(config: uvicorn.Config, path: Path) -> bool:
    """Whether uvicorn's reloader restarts the server when ``path`` changes."""
    watched = any(directory in path.parents for directory in config.reload_dirs)
    return watched and FileFilter(config)(path)


def test_main_reload_restarts_on_source_changes_only(monkeypatch, tmp_path: Path) -> None:
    """The dev server reloads on edits to its own source, wherever it is started from."""
    monkeypatch.setattr(web_app, "_settings_error", None)
    monkeypatch.setattr(
        "openscientist.settings.get_settings",
        lambda: SimpleNamespace(
            dev=SimpleNamespace(dev_mode=True),
            database=SimpleNamespace(database_url="unused"),
            auth=SimpleNamespace(app_url="http://localhost:8080"),
            bind_address="127.0.0.1",
        ),
    )
    monkeypatch.setattr(web_app, "_refuse_unsafe_database_login", lambda _url: None)
    configs: list[uvicorn.Config] = []
    monkeypatch.setattr(
        web_app.uvicorn,
        "run",
        lambda *args, **kwargs: configs.append(uvicorn.Config(*args, **kwargs)),
    )
    monkeypatch.chdir(tmp_path)
    jobs_dir = tmp_path / "jobs"

    web_app.main(host="127.0.0.1", port=9999, jobs_dir=jobs_dir)

    config = configs[0]
    source = Path(web_app.__file__).resolve()
    assert _restarts(config, source)
    assert _restarts(config, source.parent.parent / "openscientist_tools" / "code_exec.py")
    assert not _restarts(config, tmp_path / "unrelated.py")
    assert not _restarts(config, jobs_dir / "0193" / "provenance" / "plot.py")
    assert not _restarts(config, tmp_path / ".nicegui" / "storage-user-0193.json")


def test_main_non_reload_runs_with_created_app(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_app, "_settings_error", None)
    monkeypatch.setattr(
        "openscientist.settings.get_settings",
        lambda: SimpleNamespace(
            dev=SimpleNamespace(dev_mode=False),
            database=SimpleNamespace(database_url="unused"),
            auth=SimpleNamespace(app_url="http://localhost:8080"),
            bind_address="127.0.0.1",
        ),
    )
    monkeypatch.setattr(web_app, "_refuse_unsafe_database_login", lambda _url: None)

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


def test_register_nicegui_static_files_tolerates_duplicates(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_add_static_files(route: str, directory: str) -> None:
        calls.append((route, directory))
        if route == "/assets":
            raise ValueError("already registered")

    monkeypatch.setattr(web_app.app, "add_static_files", fake_add_static_files)

    web_app._register_nicegui_static_files()

    # Job files are never static, since only users who may see the job get them.
    routes = [route for route, _ in calls]
    assert routes == ["/assets"]


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


def test_configure_host_app_registers_badge_and_thinking_status_head_html_once(
    monkeypatch, tmp_path: Path
) -> None:
    """register_badge_head_html() and _inject_thinking_status_styles() must
    each run exactly once at bootstrap, the same as _register_pwa_metadata --
    all three call ui.add_head_html(shared=True), which appends to a
    process-global string with no dedup, so a second call would leak."""
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
    monkeypatch.setattr(web_app.ui, "run_with", _noop)
    _skip_page_module_import(monkeypatch)

    badge_calls: list[None] = []
    thinking_calls: list[None] = []
    monkeypatch.setattr(web_app, "register_badge_head_html", lambda: badge_calls.append(None))
    monkeypatch.setattr(
        web_app, "_inject_thinking_status_styles", lambda: thinking_calls.append(None)
    )

    web_app.create_app(tmp_path / "jobs")
    web_app.create_app(tmp_path / "jobs")  # second call must be a no-op (already configured)

    assert len(badge_calls) == 1
    assert len(thinking_calls) == 1


def _settings_with_admin_url(admin_url: str | None) -> SimpleNamespace:
    from openscientist.settings import DatabaseSettings

    database = DatabaseSettings(
        DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db",
        ADMIN_DATABASE_URL=admin_url,
    )
    return SimpleNamespace(database=database)


def test_web_refuses_to_start_without_the_admin_url(monkeypatch, tmp_path: Path) -> None:
    """The web opens admin sessions, so a missing admin URL is a configuration
    error and main() serves the error page instead of the app."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "openscientist.settings.get_settings", lambda: _settings_with_admin_url(None)
    )
    error = web_app._validate_settings()
    assert error is not None
    assert "ADMIN_DATABASE_URL" in error


def test_web_starts_with_the_admin_url(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "openscientist.settings.get_settings",
        lambda: _settings_with_admin_url("postgresql+asyncpg://admin:pass@host:5432/db"),
    )
    assert web_app._validate_settings() is None


def test_initialize_job_manager_runtime_installs_the_manager(monkeypatch, tmp_path: Path) -> None:
    """The runtime initializer installs a global manager, and a second call
    leaves the existing manager in place."""
    monkeypatch.setattr(web_app.JobManager, "recover_after_restart", lambda self: None)
    monkeypatch.setattr(web_app, "_state", web_app._AppState())

    web_app._initialize_job_manager_runtime(tmp_path / "jobs")
    manager = web_app.get_job_manager()
    assert manager.jobs_dir == tmp_path / "jobs"

    web_app._initialize_job_manager_runtime(tmp_path / "other")
    assert web_app.get_job_manager() is manager


def test_openapi_docs_load_their_assets_from_the_app() -> None:
    host_app = FastAPI(title="OpenScientist API", version="1.0.0")
    web_app._register_openapi_docs(host_app)
    client = TestClient(host_app)

    swagger = client.get("/api-docs")
    assert "/docs-assets/swagger-ui-bundle.js" in swagger.text
    assert "/docs-assets/swagger-ui.css" in swagger.text
    assert "jsdelivr" not in swagger.text
    assert "tiangolo" not in swagger.text

    redoc = client.get("/api-redoc")
    assert "/docs-assets/redoc.standalone.js" in redoc.text
    assert "jsdelivr" not in redoc.text
    assert "fonts.googleapis.com" not in redoc.text

    # The docs pages carry their own CSP (ReDoc spawns a blob: worker), so the
    # app's page policy must not be layered over them.
    for response in (swagger, redoc):
        csp = response.headers["content-security-policy"]
        assert "worker-src 'self' blob:" in csp
        assert "frame-ancestors 'none'" not in csp

    for asset in ("swagger-ui-bundle.js", "swagger-ui.css", "redoc.standalone.js"):
        response = client.get(f"/docs-assets/{asset}")
        assert response.status_code == 200
        assert len(response.content) > 100_000

    logo = client.get("/docs-assets/redoc-logo-mini.svg")
    assert logo.status_code == 200
    assert "cdn.redoc.ly" not in client.get("/docs-assets/redoc.standalone.js").text


def test_create_app_registers_the_security_headers_middleware(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_app, "_state", web_app._AppState())
    monkeypatch.setattr(web_app, "_register_robots_txt", _noop)
    monkeypatch.setattr(web_app, "_register_api_routes", _noop)
    monkeypatch.setattr(web_app, "_register_oauth_routes", _noop)
    monkeypatch.setattr(web_app, "_register_share_routes", _noop)
    monkeypatch.setattr(web_app, "_register_review_routes", _noop)
    monkeypatch.setattr(web_app, "_register_download_routes", _noop)
    monkeypatch.setattr(web_app, "_initialize_job_manager_runtime", _noop)
    monkeypatch.setattr(web_app, "_register_nicegui_static_files", _noop)
    monkeypatch.setattr(web_app, "_register_pwa_metadata", _noop)
    monkeypatch.setattr(web_app, "register_badge_head_html", _noop)
    monkeypatch.setattr(web_app, "_inject_density_styles", _noop)
    monkeypatch.setattr(web_app, "_inject_thinking_status_styles", _noop)
    monkeypatch.setattr(web_app.ui, "run_with", _noop)
    _skip_page_module_import(monkeypatch)

    host_app = web_app.create_app(tmp_path / "jobs")
    classes = [middleware.cls for middleware in host_app.user_middleware]
    assert SecurityHeadersMiddleware in classes
    assert ScannerBlockMiddleware in classes

    client = TestClient(host_app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers
