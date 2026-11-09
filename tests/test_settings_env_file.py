"""The test session reads no .env, so a deployed checkout's values never reach a test."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from openscientist.settings import clear_settings_cache, get_settings


@pytest.fixture
def staging_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A .env in the working directory, the way a deployed checkout holds one."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENSCIENTIST_EXECUTOR_IMAGE=openscientist-executor:staging\n"
        "OPENSCIENTIST_AGENT_IMAGE=openscientist-agent:staging\n"
        "OPENSCIENTIST_DEV_MODE=true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    clear_settings_cache()
    yield env_file
    clear_settings_cache()


def test_a_dotenv_in_the_working_directory_leaves_the_defaults(staging_env_file: Path) -> None:
    settings = get_settings()
    assert settings.container.executor_image == "openscientist-executor:latest"
    assert settings.container.agent_image == "openscientist-agent:latest"
    assert settings.dev.dev_mode is False


def test_the_container_manager_keeps_the_default_image(staging_env_file: Path) -> None:
    from openscientist.container_manager import ContainerManager

    assert ContainerManager().image == "openscientist-executor:latest"


@pytest.mark.usefixtures("web_main_environment")
def test_starting_the_web_app_from_a_checkout_leaves_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() loads .env into the process environment, which in a test session
    would reach every later test, so the session keeps it out."""
    from openscientist import web_app
    from tests.test_dev_mode_gate import _run_main

    names = ("OPENSCIENTIST_EXECUTOR_IMAGE", "OPENSCIENTIST_ADMIN_EMAILS")
    (tmp_path / ".env").write_text(
        "OPENSCIENTIST_EXECUTOR_IMAGE=x\nOPENSCIENTIST_ADMIN_EMAILS=admin@staging.example\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    before = {name: os.environ.get(name) for name in names}
    uvicorn_calls = _run_main(monkeypatch, tmp_path, False, "https://sci.example.com", "0.0.0.0")
    web_app.main(host="127.0.0.1", port=9999, jobs_dir=tmp_path / "jobs")
    assert uvicorn_calls
    assert {name: os.environ.get(name) for name in names} == before
