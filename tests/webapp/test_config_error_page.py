"""The configuration-error page built in a real NiceGUI client."""

from __future__ import annotations

import pytest
from nicegui import ui

from openscientist import web_app
from tests.webapp.conftest import OpenBrowser


@pytest.mark.asyncio
async def test_the_config_error_page_answers_500(browser_client: OpenBrowser) -> None:
    """A misconfigured instance reports 500, so health probes and load
    balancers see it as unhealthy. NiceGUI sends the page with the status its
    client carries when the build ends."""
    client = await browser_client(None, "/some/unknown/path")
    with client:
        web_app._render_config_error_page()
    assert client.status_code == 500
    labels = [e.text for e in client.elements.values() if isinstance(e, ui.label)]
    assert "HTTP 500 - Internal Server Error" in labels
