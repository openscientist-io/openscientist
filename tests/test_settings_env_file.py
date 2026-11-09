"""The test session reads no .env, so a deployed checkout's values never reach a test."""

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
