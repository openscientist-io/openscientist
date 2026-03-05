from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI

from openscientist import web_app


def _noop(*_args, **_kwargs) -> None:
    pass


def test_create_app_builds_host_app_once(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(web_app, "_state", web_app._AppState())
    monkeypatch.setattr(web_app, "_register_openapi_docs", _noop)
    monkeypatch.setattr(web_app, "_register_health_endpoint", _noop)
    monkeypatch.setattr(web_app, "_register_api_routes", _noop)
    monkeypatch.setattr(web_app, "_register_oauth_routes", _noop)
    monkeypatch.setattr(web_app, "_register_share_routes", _noop)
    monkeypatch.setattr(web_app, "_initialize_job_manager_runtime", _noop)
    monkeypatch.setattr(web_app, "_register_nicegui_static_files", _noop)
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
