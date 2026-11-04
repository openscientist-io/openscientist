"""At startup the web names every container image Docker does not have."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from openscientist import web_app
from openscientist.settings import get_settings
from tests.helpers import assert_names_a_build_command


def _docker(monkeypatch: pytest.MonkeyPatch, *, reachable: bool, images: set[str]) -> None:
    manager = MagicMock()
    manager.is_available.return_value = reachable
    manager.check_image_available.side_effect = lambda image: image in images
    monkeypatch.setattr(web_app, "get_container_manager", lambda: manager)


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_missing_image_is_named_with_how_to_build_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    container = get_settings().container
    _docker(monkeypatch, reachable=True, images={container.executor_image})
    with caplog.at_level(logging.INFO):
        await web_app._check_container_images()
    [message] = _errors(caplog)
    assert container.agent_image in message
    assert container.executor_image not in message
    assert_names_a_build_command(message)


async def test_present_images_log_no_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    container = get_settings().container
    _docker(monkeypatch, reachable=True, images={container.agent_image, container.executor_image})
    with caplog.at_level(logging.INFO):
        await web_app._check_container_images()
    assert _errors(caplog) == []


async def test_an_unreachable_launcher_is_not_reported_as_missing_images(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    container = get_settings().container
    _docker(monkeypatch, reachable=False, images=set())
    with caplog.at_level(logging.INFO):
        await web_app._check_container_images()
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert logged
    assert container.agent_image not in logged
    assert container.executor_image not in logged
