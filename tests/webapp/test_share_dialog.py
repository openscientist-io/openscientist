"""The share dialog is owner-gated: loading the share list requires the caller
to own the job, the dialog renders the list when the owner opens it, and the
job page builds the dialog only for the owner."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import core, ui
from nicegui.client import Client
from nicegui.page import page
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.auth import middleware as auth_middleware
from openscientist.database.models import Job, JobShare, User
from openscientist.webapp_components.components.dialogs import (
    _load_job_shares,
    render_share_dialog,
)
from openscientist.webapp_components.pages.job_detail import _create_page_dialogs
from tests.db_outage import Forwarder


@pytest.fixture(autouse=True)
def ui_slot() -> Generator[None, None, None]:
    """Create the pseudo script client, the way NiceGUI does for the first
    element outside a page request."""
    if core.script_client is None:
        core.script_mode = True
        core.script_client = Client(page("/")).__enter__()
    yield


@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Carry the signed-in user id in NiceGUI storage, as a page does."""
    storage: dict[str, object] = {}
    monkeypatch.setattr(
        auth_middleware,
        "app",
        SimpleNamespace(storage=SimpleNamespace(user=storage)),
    )

    def sign_in(user_id: str) -> None:
        storage["user_id"] = user_id

    return sign_in


def _labels(element: ui.element) -> list[str]:
    return [
        child.text for child in element.descendants() if isinstance(child, ui.label) and child.text
    ]


def _content_slot() -> ui.element:
    """The script client's content slot, guaranteed by the ui_slot fixture."""
    assert core.script_client is not None
    return core.script_client.content


@pytest_asyncio.fixture
async def seed_world(
    test_engine: AsyncEngine,
) -> AsyncGenerator[Callable[..., Awaitable[SimpleNamespace]], None]:
    """Seed an owner, a share holder and a caller with the given share level."""
    owner_ids: list[object] = []
    user_ids: list[object] = []

    async def seed(is_public: bool = False, caller_level: str | None = None) -> SimpleNamespace:
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            owner = User(email=f"owner-{uuid4().hex}@share.test", name="Share Owner")
            holder = User(email=f"holder-{uuid4().hex}@share.test", name="List Holder")
            caller = User(email=f"caller-{uuid4().hex}@share.test", name="Caller")
            session.add_all([owner, holder, caller])
            await session.flush()
            owner_ids.append(owner.id)
            user_ids.extend([owner.id, holder.id, caller.id])
            job = Job(
                owner_id=owner.id,
                research_question="Share dialog gating test",
                status="pending",
                is_public=is_public,
            )
            session.add(job)
            await session.flush()
            session.add(
                JobShare(job_id=job.id, shared_with_user_id=holder.id, permission_level="edit")
            )
            if caller_level is not None:
                session.add(
                    JobShare(
                        job_id=job.id,
                        shared_with_user_id=caller.id,
                        permission_level=caller_level,
                    )
                )
            await session.commit()
            return SimpleNamespace(owner=owner, holder=holder, caller=caller, job=job)

    yield seed

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        for owner_id in owner_ids:
            job_ids = (
                (await session.execute(select(Job.id).where(Job.owner_id == owner_id)))
                .scalars()
                .all()
            )
            await session.execute(delete(JobShare).where(JobShare.job_id.in_(job_ids)))
            await session.execute(delete(Job).where(Job.owner_id == owner_id))
        await session.execute(delete(User).where(User.id.in_(user_ids)))
        await session.commit()


@pytest.mark.asyncio
async def test_the_owner_loads_the_share_list(
    database: Forwarder,
    seed_world: Callable[..., Awaitable[SimpleNamespace]],
) -> None:
    """The owner's load returns the recipient's name, email and level."""
    world = await seed_world()

    rows = await _load_job_shares(str(world.job.id), str(world.owner.id))
    assert [share.permission_level for share, _ in rows] == ["edit"]
    assert [target.name for _, target in rows] == [world.holder.name]
    assert [target.email for _, target in rows] == [world.holder.email]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("share_level", "is_public"),
    [
        (None, False),  # signed-in stranger, private job
        (None, True),  # public-job reader
        ("view", True),  # view share recipient
        ("edit", True),  # edit share recipient
    ],
)
async def test_a_non_owner_loads_no_shares(
    database: Forwarder,
    seed_world: Callable[..., Awaitable[SimpleNamespace]],
    share_level: str | None,
    is_public: bool,
) -> None:
    """A stranger, public reader or share recipient gets no rows and no emails."""
    world = await seed_world(is_public=is_public, caller_level=share_level)

    rows = await _load_job_shares(str(world.job.id), str(world.caller.id))
    assert rows == []


@pytest.mark.asyncio
async def test_the_dialog_shows_the_share_list_when_the_owner_opens_it(
    database: Forwarder,
    seed_world: Callable[..., Awaitable[SimpleNamespace]],
    signed_in: Callable[[str], None],
) -> None:
    """Opening the dialog as the owner loads and renders the current share list."""
    world = await seed_world()
    signed_in(str(world.owner.id))

    with _content_slot():
        dialog = render_share_dialog(str(world.job.id))
        show_handlers = [
            listener.handler
            for listener in dialog._event_listeners.values()
            if listener.type == "show" and listener.handler is not None
        ]
        assert show_handlers, "opening the dialog must reload the share list"
        await show_handlers[0]()

        labels = _labels(dialog)
        badges = [
            child.text
            for child in dialog.descendants()
            if isinstance(child, ui.badge) and child.text
        ]
    assert world.holder.name in labels
    assert world.holder.email in labels
    assert "edit" in badges


@pytest.mark.asyncio
async def test_the_dialog_shows_no_shares_when_a_non_owner_opens_it(
    database: Forwarder,
    seed_world: Callable[..., Awaitable[SimpleNamespace]],
    signed_in: Callable[[str], None],
) -> None:
    """A non-owner's open renders no recipient names or emails."""
    world = await seed_world(is_public=True)
    signed_in(str(world.caller.id))

    with _content_slot():
        dialog = render_share_dialog(str(world.job.id))
        show_handlers = [
            listener.handler
            for listener in dialog._event_listeners.values()
            if listener.type == "show" and listener.handler is not None
        ]
        assert show_handlers, "opening the dialog must reload the share list"
        await show_handlers[0]()

        labels = _labels(dialog)
    assert world.holder.name not in labels
    assert world.holder.email not in labels


@pytest.mark.asyncio
async def test_the_job_page_builds_no_share_dialog_for_a_non_owner() -> None:
    """A non-owner's job page has no share dialog, so forged events find no target."""
    with _content_slot():
        share_dialog, delete_dialog, notifications_dialog = _create_page_dialogs(
            job_id=str(uuid4()),
            job_manager=None,
            user_id=str(uuid4()),
            is_owner=False,
        )
    assert share_dialog is None
    assert delete_dialog is not None
    assert notifications_dialog is not None


@pytest.mark.asyncio
async def test_the_job_page_builds_the_share_dialog_for_the_owner() -> None:
    """The owner's job page keeps its share dialog."""
    with _content_slot():
        share_dialog, _, _ = _create_page_dialogs(
            job_id=str(uuid4()),
            job_manager=None,
            user_id=str(uuid4()),
            is_owner=True,
        )
    assert share_dialog is not None
