"""Signing out leaves no JavaScript timeout behind, because the page it runs
on navigates away before it could answer."""

import asyncio

import pytest
from nicegui.testing import user_simulation
from nicegui.testing.user_interaction import UserInteraction

from openscientist.webapp_components.components.navigation import render_navigator


@pytest.mark.asyncio
@pytest.mark.parametrize("button_index", [0, 1], ids=["mobile", "desktop"])
async def test_sign_out_logs_no_javascript_timeout(
    button_index: int, caplog: pytest.LogCaptureFixture
) -> None:
    async with user_simulation(root=render_navigator) as browser:
        await browser.open("/")
        buttons = sorted(browser.find("Logout").elements, key=lambda button: button.id)
        assert len(buttons) == 2
        UserInteraction(browser, {buttons[button_index]}, "Logout").click()
        # Past the simulated client's one-second JavaScript response timeout.
        await asyncio.sleep(1.5)
    assert "JavaScript did not respond" not in caplog.text
