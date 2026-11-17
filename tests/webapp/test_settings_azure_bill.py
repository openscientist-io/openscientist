"""The admin Settings tab says which bill the project spend limits count under Foundry."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from nicegui.testing import user_simulation

from openscientist.settings import clear_settings_cache
from openscientist.webapp_components.pages.settings_panel import render_settings_panel

_LEDGER_ONLY = (
    "With Foundry, the project spend limits count only OpenScientist's own cost record, "
    "priced from token counts, and never the Azure bill."
)
_AZURE_LOGIN = {
    "AZURE_TENANT_ID": "tenant",
    "AZURE_CLIENT_ID": "client",
    "AZURE_CLIENT_SECRET": "secret",
}
_AZURE_KEYS = ("AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", *_AZURE_LOGIN)


@pytest.fixture
def foundry(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Foundry with no Azure billing setting, for a test to add the ones it needs."""
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "foundry")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "resource")
    for key in _AZURE_KEYS:
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()
    yield monkeypatch
    clear_settings_cache()


async def _settings_tab(admin_token: str, expected: str) -> None:
    # The Settings tab alone, so the Billing tab's own provider note cannot match.
    async with user_simulation(root=render_settings_panel) as browser:
        browser.http_client.cookies.set("session_token", admin_token)
        await browser.open("/")
        await browser.should_see("Agent CPU", retries=50)
        await browser.should_see(expected)


@pytest.mark.asyncio
async def test_no_billing_settings_name_the_optional_billing_keys(
    admin_token: str, foundry: pytest.MonkeyPatch
) -> None:
    await _settings_tab(
        admin_token,
        f"{_LEDGER_ONLY} AZURE_SUBSCRIPTION_ID, AZURE_TENANT_ID, AZURE_CLIENT_ID and "
        "AZURE_CLIENT_SECRET are optional billing settings that let them also count the "
        "Azure bill from Azure Cost Management.",
    )


@pytest.mark.asyncio
async def test_a_subscription_alone_names_the_missing_azure_login(
    admin_token: str, foundry: pytest.MonkeyPatch
) -> None:
    foundry.setenv("AZURE_SUBSCRIPTION_ID", "subscription")
    clear_settings_cache()
    await _settings_tab(
        admin_token,
        f"{_LEDGER_ONLY} AZURE_SUBSCRIPTION_ID is set, but AZURE_TENANT_ID, AZURE_CLIENT_ID "
        "and AZURE_CLIENT_SECRET are not. Set all four to count the Azure bill.",
    )


@pytest.mark.asyncio
async def test_full_billing_settings_say_the_whole_subscription_is_counted(
    admin_token: str, foundry: pytest.MonkeyPatch
) -> None:
    foundry.setenv("AZURE_SUBSCRIPTION_ID", "subscription")
    for key, value in _AZURE_LOGIN.items():
        foundry.setenv(key, value)
    clear_settings_cache()
    await _settings_tab(
        admin_token,
        "With Foundry, the project spend limits count the larger of OpenScientist's own cost "
        "record and the Azure bill of the whole subscription, which includes the spend of "
        "every other deployment sharing it. Set AZURE_RESOURCE_GROUP to count one resource "
        "group only.",
    )


@pytest.mark.asyncio
async def test_another_provider_shows_no_azure_bill_line(
    admin_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    clear_settings_cache()
    try:
        async with user_simulation(root=render_settings_panel) as browser:
            browser.http_client.cookies.set("session_token", admin_token)
            await browser.open("/")
            await browser.should_see("Agent CPU", retries=50)
            await browser.should_not_see("Azure bill")
    finally:
        clear_settings_cache()
