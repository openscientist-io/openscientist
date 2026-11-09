"""The web's startup logging setup, run through main() with uvicorn neutralised."""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from openscientist import web_app


def _run_main(monkeypatch, tmp_path: Path) -> None:
    """main() up to uvicorn.run, with the settings and the server neutralised."""
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
    monkeypatch.setattr(web_app, "create_app", lambda jobs_dir=None: FastAPI())
    monkeypatch.setattr(web_app.uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.chdir(tmp_path)
    web_app.main(host="127.0.0.1", port=9999, jobs_dir=tmp_path / "jobs")


@pytest.mark.usefixtures("web_main_environment")
def test_main_quiets_the_http_client_loggers(monkeypatch, tmp_path: Path) -> None:
    """The launcher status poll runs on the launcher client's httpx logger, so the
    web keeps httpx and httpcore at warnings."""
    _run_main(monkeypatch, tmp_path)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
