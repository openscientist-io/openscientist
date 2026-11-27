"""Validating MCP server registrations and storing them through the admin login."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.admin.mcp_servers import (
    McpServerRegistration,
    list_mcp_servers,
    parse_registration,
    register_mcp_server,
)
from openscientist.database.models import McpServer, User
from openscientist.database.session import get_admin_session
from tests.db_outage import Forwarder


def _valid(
    slug: str = "pubchem", name: str = "PubChem", url: str = "https://mcp.example.org/mcp"
) -> McpServerRegistration:
    registration = parse_registration(slug, name, url)
    assert isinstance(registration, McpServerRegistration), registration
    return registration


@pytest.mark.parametrize(
    ("url", "stored"),
    [
        ("https://mcp.example.org/mcp", "https://mcp.example.org/mcp"),
        ("http://localhost:8080/sse", "http://localhost:8080/sse"),
        ("HTTPS://MCP.Example.org", "https://mcp.example.org/"),
        ("http://[::1]:9000/mcp?x=1", "http://[::1]:9000/mcp?x=1"),
    ],
)
def test_an_http_or_https_url_is_accepted_in_its_normalised_form(url: str, stored: str) -> None:
    assert _valid(url=url).url == stored


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://mcp.example.org/", "URL must start with http:// or https://"),
        ("file:///etc/passwd", "URL must start with http:// or https://"),
        ("javascript:alert(1)", "URL must start with http:// or https://"),
        ("ws://mcp.example.org/", "URL must start with http:// or https://"),
        ("mcp.example.org/mcp", "URL must start with http:// or https://"),
        ("https://", "URL must name a host"),
        ("http:///mcp", "URL must name a host"),
        ("https://:443/mcp", "URL must name a host"),
        ("https://user:secret@mcp.example.org/", "URL must not contain a user name or password"),
        ("https://token@mcp.example.org/", "URL must not contain a user name or password"),
        ("http://[::1/", "URL is not a valid HTTP(S) URL"),
        ("https://mcp.example.org:99999/", "URL is not a valid HTTP(S) URL"),
        ("", "URL is required"),
    ],
)
def test_a_url_that_is_not_a_plain_http_url_is_refused(url: str, message: str) -> None:
    assert parse_registration("pubchem", "PubChem", url) == [message]


@pytest.mark.parametrize(
    "url",
    [
        " https://mcp.example.org/",
        "https://mcp.example.org/ ",
        "https://mcp.example.org/a b",
        "https://mcp .example.org/",
        "https://mcp.example.org/\t",
        "https://mcp.example.org/\n",
        "https://mcp.example.org/\r\nX: y",
        "https://mcp.example.org/\u00a0",
        "https://mcp.example.org/\u2003",
        "https://mcp.example.org/\u200b",
        "https://mcp.example.org/\x00",
    ],
    ids=repr,
)
def test_a_url_with_any_whitespace_or_control_character_is_refused(url: str) -> None:
    assert parse_registration("pubchem", "PubChem", url) == [
        "URL must not contain spaces, line breaks or control characters"
    ]


def test_an_overlong_url_is_refused() -> None:
    url = "https://mcp.example.org/" + "a" * 2048
    assert parse_registration("pubchem", "PubChem", url) == ["URL must be at most 2048 characters"]


@pytest.mark.parametrize("slug", ["a", "pubchem", "pub-chem-2", "0" * 64, "  padded  "])
def test_a_lowercase_slug_is_accepted(slug: str) -> None:
    assert _valid(slug=slug).slug == slug.strip()


@pytest.mark.parametrize(
    "slug", ["PubChem", "-pubchem", "pubchem-", "pub chem", "pub_chem", "pub.chem", "a" * 65, "é"]
)
def test_a_slug_outside_the_pattern_is_refused(slug: str) -> None:
    result = parse_registration(slug, "PubChem", "https://mcp.example.org/")
    assert isinstance(result, list)
    [message] = result
    assert message.startswith("Slug must be 1 to 64 lowercase letters")


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "Display name is required"),
        ("   ", "Display name is required"),
        ("Pub\nChem", "Display name must not contain line breaks or control characters"),
        ("x" * 201, "Display name must be at most 200 characters"),
    ],
)
def test_a_bad_display_name_is_refused(name: str, message: str) -> None:
    assert parse_registration("pubchem", name, "https://mcp.example.org/") == [message]


def test_every_wrong_field_is_reported() -> None:
    assert parse_registration(None, None, None) == [
        "Slug is required",
        "Display name is required",
        "URL is required",
    ]


@pytest_asyncio.fixture
async def admin_id(database: Forwarder, test_engine: AsyncEngine) -> AsyncIterator[UUID]:
    """A user to record as the registrant, with an empty catalog."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        await db.execute(delete(McpServer))
        admin = User(email=f"admin-{uuid4().hex}@mcp.test", name="Admin", is_approved=True)
        db.add(admin)
        await db.commit()
    yield admin.id
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(McpServer))
        await db.execute(delete(User).where(User.id == admin.id))
        await db.commit()


async def _stored(test_engine: AsyncEngine) -> list[McpServer]:
    async with AsyncSession(test_engine) as db:
        return list((await db.execute(select(McpServer).order_by(McpServer.slug))).scalars())


@pytest.mark.asyncio
async def test_a_registration_is_stored_inactive_and_listed(admin_id: UUID) -> None:
    async with get_admin_session() as session:
        assert await register_mcp_server(session, _valid(), admin_id) == (
            True,
            'Registered "PubChem" as inactive',
        )
    async with get_admin_session() as session:
        [listed] = await list_mcp_servers(session)

    assert (listed.slug, listed.display_name, listed.url) == (
        "pubchem",
        "PubChem",
        "https://mcp.example.org/mcp",
    )
    assert listed.is_active is False
    assert listed.created_by == admin_id


@pytest.mark.asyncio
async def test_servers_are_listed_by_slug(admin_id: UUID) -> None:
    for slug in ("zeta", "alpha", "mid"):
        async with get_admin_session() as session:
            saved, _ = await register_mcp_server(session, _valid(slug=slug), admin_id)
            assert saved
    async with get_admin_session() as session:
        assert [server.slug for server in await list_mcp_servers(session)] == [
            "alpha",
            "mid",
            "zeta",
        ]


@pytest.mark.asyncio
async def test_a_taken_slug_adds_no_row(admin_id: UUID, test_engine: AsyncEngine) -> None:
    async with get_admin_session() as session:
        await register_mcp_server(session, _valid(), admin_id)
    async with get_admin_session() as session:
        result = await register_mcp_server(
            session, _valid(name="Other", url="https://other.example.org/"), admin_id
        )

    assert result == (False, 'An MCP server with the slug "pubchem" is already registered')
    [stored] = await _stored(test_engine)
    assert stored.display_name == "PubChem"


@pytest.mark.asyncio
async def test_the_session_stays_usable_after_a_taken_slug(admin_id: UUID) -> None:
    async with get_admin_session() as session:
        await register_mcp_server(session, _valid(), admin_id)
        saved, _ = await register_mcp_server(session, _valid(), admin_id)
        assert not saved
        saved, _ = await register_mcp_server(session, _valid(slug="other"), admin_id)
        assert saved
        assert [server.slug for server in await list_mcp_servers(session)] == ["other", "pubchem"]


async def _insert_waiter(engine: AsyncEngine, deadline_seconds: float) -> None:
    """Wait until a backend waits on another transaction, as an insert does on
    an uncommitted row holding the same unique key."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        async with AsyncSession(engine) as db:
            waiting = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'transactionid' AND NOT granted"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"no insert waited within {deadline_seconds} s")


@pytest.mark.asyncio
async def test_a_slug_registered_concurrently_is_reported_as_taken(
    admin_id: UUID, test_engine: AsyncEngine
) -> None:
    async with AsyncSession(test_engine) as holder, holder.begin():
        holder.add(
            McpServer(slug="pubchem", display_name="First", url="https://first.example.org/")
        )
        await holder.flush()

        async def register() -> tuple[bool, str]:
            async with get_admin_session() as session:
                return await register_mcp_server(session, _valid(), admin_id)

        racing = asyncio.create_task(register())
        await _insert_waiter(test_engine, deadline_seconds=10)

    assert await racing == (False, 'An MCP server with the slug "pubchem" is already registered')
    assert [server.display_name for server in await _stored(test_engine)] == ["First"]


@pytest.mark.asyncio
async def test_a_registrant_deleted_meanwhile_is_reported_and_adds_no_row(
    admin_id: UUID, test_engine: AsyncEngine
) -> None:
    async with get_admin_session() as session:
        saved, message = await register_mcp_server(session, _valid(), uuid4())

    assert (saved, message) == (False, "The MCP server could not be registered")
    assert await _stored(test_engine) == []


@pytest.mark.asyncio
async def test_deleting_the_registrant_keeps_the_server(
    admin_id: UUID, test_engine: AsyncEngine
) -> None:
    async with get_admin_session() as session:
        await register_mcp_server(session, _valid(), admin_id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == admin_id))
        await db.commit()

    [stored] = await _stored(test_engine)
    assert stored.created_by is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("is_active", True),
        ("slug", "Not A Slug"),
        ("display_name", " padded "),
        ("url", "ftp://mcp.example.org/"),
        ("url", "https://mcp.example.org/a b"),
        ("url", "https://user@mcp.example.org/"),
        ("url", "https:///mcp"),
    ],
)
async def test_the_table_refuses_a_row_the_validation_would_refuse(
    admin_id: UUID, test_engine: AsyncEngine, column: str, value: object
) -> None:
    """The checks hold even for a writer that skips the validation."""
    row = {
        "slug": "pubchem",
        "display_name": "PubChem",
        "url": "https://mcp.example.org/",
        "is_active": False,
        column: value,
    }
    async with AsyncSession(test_engine) as db:
        db.add(McpServer(**row))
        with pytest.raises(IntegrityError, match="ck_mcp_servers_"):
            await db.commit()
