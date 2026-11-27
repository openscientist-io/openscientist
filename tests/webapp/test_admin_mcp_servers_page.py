"""Registering MCP servers from the admin page's MCP Servers tab."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from nicegui.testing import User as SimulatedBrowser
from nicegui.testing import user_simulation
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Administrator, McpServer, Session, User
from openscientist.webapp_components.pages import mcp_servers_panel
from openscientist.webapp_components.pages.admin import admin_page
from openscientist.webapp_components.pages.mcp_servers_panel import render_mcp_servers_panel
from openscientist.webapp_components.utils.permissions import AdminAccess
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage

_STALE = "Your admin session is no longer valid"
REVOCATIONS = ["demoted", "session_deleted"]


@dataclass(frozen=True)
class _People:
    admin: UUID
    admin_token: str
    member: UUID
    member_token: str


@pytest_asyncio.fixture
async def people(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[_People]:
    """A signed-in admin and a signed-in user who is not an admin, with an empty catalog."""
    expires_at = datetime.now(UTC) + timedelta(days=1)
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        await db.execute(delete(McpServer))
        admin = User(email=f"admin-{uuid4().hex}@mcp.test", name="Admin", is_approved=True)
        member = User(email=f"member-{uuid4().hex}@mcp.test", name="Member", is_approved=True)
        db.add_all([admin, member])
        await db.flush()
        admin_login, admin_token = Session.issue(user_id=admin.id, expires_at=expires_at)
        member_login, member_token = Session.issue(user_id=member.id, expires_at=expires_at)
        db.add_all([Administrator(user_id=admin.id), admin_login, member_login])
        await db.commit()
    yield _People(admin.id, admin_token, member.id, member_token)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(McpServer))
        await db.execute(delete(User).where(User.id.in_([admin.id, member.id])))
        await db.commit()


async def _stored(test_engine: AsyncEngine) -> list[McpServer]:
    async with AsyncSession(test_engine) as db:
        return list((await db.execute(select(McpServer).order_by(McpServer.slug))).scalars())


async def _add(test_engine: AsyncEngine, slug: str, display_name: str) -> None:
    async with AsyncSession(test_engine) as db:
        db.add(McpServer(slug=slug, display_name=display_name, url=f"https://{slug}.example.org/"))
        await db.commit()


async def _revoke(test_engine: AsyncEngine, admin: UUID, revocation: str) -> None:
    """Take the admin role or the login session away from ``admin``."""
    model = Administrator if revocation == "demoted" else Session
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(model).where(model.user_id == admin))
        await db.commit()


def _rows(browser: SimulatedBrowser) -> list[dict[str, Any]]:
    return [row for table in browser.find(ui.table).elements for row in table.rows]


def _fill(browser: SimulatedBrowser, slug: str, display_name: str, url: str) -> None:
    browser.find(kind=ui.input, content="Slug").type(slug)
    browser.find(kind=ui.input, content="Display name").type(display_name)
    browser.find(kind=ui.input, content="URL").type(url)


async def _open(browser: SimulatedBrowser, token: str) -> None:
    browser.http_client.cookies.set("session_token", token)
    await browser.open("/")
    await browser.should_see("Register MCP Server", retries=50)


@pytest.mark.asyncio
async def test_an_empty_catalog_says_so(people: _People) -> None:
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.admin))
    ) as browser:
        await _open(browser, people.admin_token)
        await browser.should_see("No MCP servers registered yet.", retries=50)


@pytest.mark.asyncio
async def test_an_admin_registers_a_server_that_stays_inactive_and_listed_after_a_reload(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.admin))
    ) as browser:
        await _open(browser, people.admin_token)
        _fill(browser, "pubchem", "PubChem", "https://mcp.example.org/mcp")
        browser.find(kind=ui.button, content="Register").click()
        await browser.should_see('Registered "PubChem" as inactive', retries=50)
        await browser.should_see(kind=ui.table, retries=50)
        [row] = _rows(browser)
        assert (row["slug"], row["display_name"], row["url"], row["status"]) == (
            "pubchem",
            "PubChem",
            "https://mcp.example.org/mcp",
            "Inactive",
        )
        [slug_input] = browser.find(kind=ui.input, content="Slug").elements
        assert slug_input.value == ""

        await browser.open("/")
        await browser.should_see(kind=ui.table, retries=50)
        assert [row["slug"] for row in _rows(browser)] == ["pubchem"]

    [stored] = await _stored(test_engine)
    assert stored.is_active is False
    assert stored.created_by == people.admin


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://mcp.example.org/", "URL must start with http:// or https://"),
        ("javascript:alert(1)", "URL must start with http:// or https://"),
        ("https://", "URL must name a host"),
        ("https://mcp.example.org/a b", "URL must not contain spaces"),
        ("https://mcp.example.org/\t", "URL must not contain spaces"),
        ("https://user:secret@mcp.example.org/", "URL must not contain a user name or password"),
    ],
    ids=["ftp", "javascript", "no-host", "space", "tab", "credentials"],
)
async def test_a_bad_url_is_explained_and_stores_nothing(
    people: _People, test_engine: AsyncEngine, url: str, message: str
) -> None:
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.admin))
    ) as browser:
        await _open(browser, people.admin_token)
        _fill(browser, "pubchem", "PubChem", url)
        browser.find(kind=ui.button, content="Register").click()
        await browser.should_see("Not registered", retries=50)
        await browser.should_see(message)
        await browser.should_see("No MCP servers registered yet.")

    assert await _stored(test_engine) == []


@pytest.mark.asyncio
async def test_a_bad_slug_and_a_missing_name_are_explained_and_store_nothing(
    people: _People, test_engine: AsyncEngine
) -> None:
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.admin))
    ) as browser:
        await _open(browser, people.admin_token)
        browser.find(kind=ui.input, content="Slug").type("Pub Chem")
        browser.find(kind=ui.input, content="URL").type("https://mcp.example.org/")
        browser.find(kind=ui.button, content="Register").click()
        await browser.should_see("Slug must be 1 to 64 lowercase letters", retries=50)
        await browser.should_see("Display name is required")

    assert await _stored(test_engine) == []


@pytest.mark.asyncio
async def test_a_taken_slug_is_explained_and_adds_no_row(
    people: _People, test_engine: AsyncEngine
) -> None:
    await _add(test_engine, "pubchem", "Original")
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.admin))
    ) as browser:
        await _open(browser, people.admin_token)
        await browser.should_see(kind=ui.table, retries=50)
        _fill(browser, "pubchem", "Copy", "https://copy.example.org/")
        browser.find(kind=ui.button, content="Register").click()
        await browser.should_see(
            'An MCP server with the slug "pubchem" is already registered', retries=50
        )
        assert [row["display_name"] for row in _rows(browser)] == ["Original"]

    assert [server.display_name for server in await _stored(test_engine)] == ["Original"]


@pytest.mark.asyncio
async def test_a_non_admin_reads_nothing_and_registers_nothing(
    people: _People, test_engine: AsyncEngine
) -> None:
    await _add(test_engine, "hidden-server", "Hidden from members")
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.member))
    ) as browser:
        await _open(browser, people.member_token)
        await browser.should_see(_STALE, retries=50)
        await browser.should_not_see("Hidden from members")
        _fill(browser, "member-server", "Member", "https://member.example.org/")
        browser.find(kind=ui.button, content="Register").click()
        await browser.should_see(_STALE, retries=50)
        await browser.should_not_see("Hidden from members")

    assert [server.slug for server in await _stored(test_engine)] == ["hidden-server"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", REVOCATIONS)
async def test_registering_after_losing_admin_rights_stores_nothing_and_hides_the_catalog(
    people: _People, test_engine: AsyncEngine, revocation: str
) -> None:
    await _add(test_engine, "existing", "Existing server")
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.admin))
    ) as browser:
        await _open(browser, people.admin_token)
        await browser.should_see("Existing server", retries=50)
        await _revoke(test_engine, people.admin, revocation)
        _fill(browser, "late", "Late", "https://late.example.org/")
        browser.find(kind=ui.button, content="Register").click()
        await browser.should_see(_STALE, retries=50)
        await browser.should_not_see("Existing server")

    assert [server.slug for server in await _stored(test_engine)] == ["existing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", REVOCATIONS)
async def test_reloading_after_losing_admin_rights_reads_nothing(
    people: _People, test_engine: AsyncEngine, revocation: str
) -> None:
    await _add(test_engine, "existing", "Existing server")
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.admin))
    ) as browser:
        await _open(browser, people.admin_token)
        await browser.should_see("Existing server", retries=50)
        await _revoke(test_engine, people.admin, revocation)
        await _add(test_engine, "fresh", "Fresh secret server")
        await browser.open("/")
        await browser.should_see(_STALE, retries=50)
        await browser.should_not_see("Existing server")
        await browser.should_not_see("Fresh secret server")


@pytest.mark.asyncio
async def test_every_read_and_write_checks_admin_rights_again(
    people: _People, test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first load, the registration and the reload after it each check."""
    checks: list[bool] = []
    check = mcp_servers_panel.admin_access

    async def counted() -> AdminAccess | None:
        access = await check()
        checks.append(access is not None)
        return access

    monkeypatch.setattr(mcp_servers_panel, "admin_access", counted)
    async with user_simulation(
        root=partial(render_mcp_servers_panel, str(people.admin))
    ) as browser:
        await _open(browser, people.admin_token)
        await browser.should_see("No MCP servers registered yet.", retries=50)
        assert checks == [True]
        _fill(browser, "pubchem", "PubChem", "https://mcp.example.org/")
        browser.find(kind=ui.button, content="Register").click()
        await browser.should_see(kind=ui.table, retries=50)
        assert checks == [True, True, True]

    assert [server.slug for server in await _stored(test_engine)] == ["pubchem"]


def _tab_labels(client: Client) -> list[str]:
    return [
        str(element.props.get("label"))
        for element in client.elements.values()
        if isinstance(element, ui.tab)
    ]


@pytest.mark.asyncio
async def test_the_admin_page_has_an_mcp_servers_tab_beside_the_others(
    people: _People, served_page: ServedPage
) -> None:
    _, labels = await served_page(people.admin_token, admin_page, _tab_labels)
    assert labels[-2:] == ["Settings", "MCP Servers"]


@pytest.mark.asyncio
async def test_a_non_admin_gets_no_admin_page_and_no_mcp_servers_tab(
    people: _People, served_page: ServedPage
) -> None:
    _, labels = await served_page(people.member_token, admin_page, _tab_labels)
    assert "MCP Servers" not in labels
