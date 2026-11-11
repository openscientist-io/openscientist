"""The share and delete dialogs of the jobs list and of the job page open
with their content already rendered.

The share list, the visibility switch and the delete dialog's job state are
read from the database before the dialog's ``open()`` runs, so the dialog
never appears empty and fills in a moment later. A spy on NiceGUI's
``ui.dialog.open`` records what each dialog holds at the moment the open is
sent; the page events are fired the way Quasar and NiceGUI would deliver
them."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, events, storage, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, JobShare, Session, User
from openscientist.job.types import JobStatus
from openscientist.job_manager import JobManager
from openscientist.webapp_components.pages.job_detail import job_detail_page
from openscientist.webapp_components.pages.jobs_list import jobs_page
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, in_event_context


@dataclass
class OpenSnapshot:
    """What a dialog held at the moment NiceGUI's ``open()`` ran on it."""

    labels: list[str]
    badges: list[str]
    buttons: list[str]
    switches: list[bool] = field(default_factory=list)


def _open_snapshots(monkeypatch: pytest.MonkeyPatch) -> list[OpenSnapshot]:
    """Record what every dialog holds when its raw ``open()`` runs."""
    snaps: list[OpenSnapshot] = []
    real_open = ui.dialog.open

    def spy(self: ui.dialog) -> None:
        snaps.append(
            OpenSnapshot(
                labels=[e.text for e in self.descendants() if isinstance(e, ui.label) and e.text],
                badges=[e.text for e in self.descendants() if isinstance(e, ui.badge) and e.text],
                buttons=[e.text for e in self.descendants() if isinstance(e, ui.button) and e.text],
                switches=[bool(e.value) for e in self.descendants() if isinstance(e, ui.switch)],
            )
        )
        real_open(self)

    monkeypatch.setattr(ui.dialog, "open", spy)
    return snaps


async def _fire_table_event(table: ui.table, event_type: str, args: str, client: Client) -> None:
    """Emit a table slot event the way Quasar's ``$emit`` would and await it."""
    listener = next(
        (entry for entry in table._event_listeners.values() if entry.type == event_type), None
    )
    assert listener is not None and listener.handler is not None, f"no {event_type} listener"
    storage.request_contextvar.set(listener.request)
    with client:
        with table.parent_slot or nullcontext():
            result = listener.handler(
                events.GenericEventArguments(sender=table, client=client, args=args)
            )
            if inspect.isawaitable(result):
                await result


async def _fire_click(button: ui.button, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire the button's click the way NiceGUI's socket handler does and await it.

    A deployment schedules page handlers on the app's background tasks, which
    only exist while ui.run owns the loop, so the test captures the scheduled
    coroutine and awaits it here."""
    scheduled: list[Awaitable[None]] = []

    def _capture(
        awaitable: Awaitable[None],
        *,
        name: str = "",
        context: AbstractContextManager[object] | None = None,
    ) -> None:
        scheduled.append(in_event_context(awaitable, context))

    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    handler = listener.handler
    storage.request_contextvar.set(listener.request)
    with monkeypatch.context() as capture:
        capture.setattr(background_tasks, "create_or_defer", _capture)
        with client:
            with button.parent_slot or nullcontext():
                handler(None)
    if scheduled:
        await scheduled[0]


def _owned_table(client: Client) -> ui.table:
    """The My Jobs table, which is the one without an Owner column."""
    for table in (e for e in client.elements.values() if isinstance(e, ui.table)):
        if not any(column["name"] == "owner" for column in table.columns):
            return table
    raise AssertionError("the jobs page has no owned-jobs table")


def _action_button(client: Client, icon: str) -> ui.button | None:
    """The job action button with the given icon, if the page rendered one."""
    return next(
        (
            e
            for e in client.elements.values()
            if isinstance(e, ui.button) and e.props.get("icon") == icon
        ),
        None,
    )


@pytest_asyncio.fixture
async def dialog_world(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """An approved owner and recipient, the owner's running job shared with
    the recipient for viewing, the owner's plain completed job, and the
    owner's live session, all committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@prerender.test", name="Owner", is_approved=True)
        holder = User(email=f"holder-{uuid4().hex}@prerender.test", name="Holder", is_approved=True)
        db.add_all([owner, holder])
        await db.flush()
        shared_job = Job(owner_id=owner.id, research_question="Shared question", status="running")
        plain_job = Job(owner_id=owner.id, research_question="Plain question", status="completed")
        db.add_all([shared_job, plain_job])
        await db.flush()
        db.add(
            JobShare(job_id=shared_job.id, shared_with_user_id=holder.id, permission_level="view")
        )
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {
        "shared_job": str(shared_job.id),
        "plain_job": str(plain_job.id),
        "holder_name": holder.name,
        "holder_email": holder.email,
        "token": str(login.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(JobShare).where(JobShare.job_id == shared_job.id))
        await db.execute(delete(Job).where(Job.id.in_([shared_job.id, plain_job.id])))
        await db.execute(delete(User).where(User.id.in_([owner.id, holder.id])))
        await db.commit()


@pytest.fixture
def job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    """The page and the delete dialog read the latest job state from it."""
    state = SimpleNamespace(status=JobStatus.RUNNING)

    async def get_job(job_id: str) -> SimpleNamespace:
        return state

    manager = SimpleNamespace(
        jobs_dir=tmp_path,
        get_job=get_job,
        ACTIVE_STATUSES=JobManager.ACTIVE_STATUSES,
        state=state,
    )
    monkeypatch.setattr(web_app._state, "job_manager", manager)
    return manager


@pytest.mark.asyncio
async def test_the_jobs_list_share_dialog_opens_with_its_share_list(
    database: Forwarder,
    dialog_world: dict[str, str],
    job_manager: SimpleNamespace,
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Share on the jobs list opens the dialog with the share list and the
    visibility already rendered in it."""
    client, _ = await served_page(dialog_world["token"], jobs_page, lambda client: None)
    snaps = _open_snapshots(monkeypatch)

    await _fire_table_event(_owned_table(client), "shareJob", dialog_world["shared_job"], client)

    assert len(snaps) == 1, f"expected one opened dialog, saw {len(snaps)}"
    snap = snaps[0]
    assert "Share Job" in snap.labels
    assert "Add New Share" in snap.labels
    assert dialog_world["holder_name"] in snap.labels
    assert dialog_world["holder_email"] in snap.labels
    assert "view" in snap.badges


@pytest.mark.asyncio
async def test_the_jobs_list_delete_dialog_opens_with_its_content(
    database: Forwarder,
    dialog_world: dict[str, str],
    job_manager: SimpleNamespace,
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete on the jobs list opens the dialog with its confirmation text and
    buttons already rendered in it."""
    client, _ = await served_page(dialog_world["token"], jobs_page, lambda client: None)
    snaps = _open_snapshots(monkeypatch)

    await _fire_table_event(_owned_table(client), "deleteJob", dialog_world["shared_job"], client)

    assert len(snaps) == 1, f"expected one opened dialog, saw {len(snaps)}"
    snap = snaps[0]
    assert "Delete Job" in snap.labels
    assert "This job is currently active and will be cancelled before deletion." in snap.labels
    assert (
        "This action cannot be undone. All job data and findings will be permanently deleted."
        in snap.labels
    )
    assert "Delete" in snap.buttons
    assert "Cancel" in snap.buttons


@pytest.mark.asyncio
async def test_the_job_detail_share_dialog_opens_with_its_empty_state(
    database: Forwarder,
    dialog_world: dict[str, str],
    job_manager: SimpleNamespace,
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Share on the job page opens the dialog with its empty state and the
    loaded visibility already rendered in it."""
    client, _ = await served_page(
        dialog_world["token"],
        lambda: job_detail_page(dialog_world["plain_job"]),
        lambda client: None,
    )
    snaps = _open_snapshots(monkeypatch)
    button = _action_button(client, "share")
    assert button is not None, "the owner's job page has no share button"

    await _fire_click(button, client, monkeypatch)

    assert len(snaps) == 1, f"expected one opened dialog, saw {len(snaps)}"
    snap = snaps[0]
    assert "Share Job" in snap.labels
    assert "No shares yet" in snap.labels
    assert snap.switches == [False]


@pytest.mark.asyncio
async def test_the_job_detail_delete_dialog_opens_with_its_content(
    database: Forwarder,
    dialog_world: dict[str, str],
    job_manager: SimpleNamespace,
    served_page: ServedPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete on the job page opens the dialog with its confirmation text and
    buttons already rendered in it."""
    job_manager.state.status = JobStatus.COMPLETED
    client, _ = await served_page(
        dialog_world["token"],
        lambda: job_detail_page(dialog_world["plain_job"]),
        lambda client: None,
    )
    snaps = _open_snapshots(monkeypatch)
    button = _action_button(client, "delete")
    assert button is not None, "the owner's job page has no delete button"

    await _fire_click(button, client, monkeypatch)

    assert len(snaps) == 1, f"expected one opened dialog, saw {len(snaps)}"
    snap = snaps[0]
    assert "Delete Job" in snap.labels
    assert (
        "This action cannot be undone. All job data and findings will be permanently deleted."
        in snap.labels
    )
    assert "This job is currently active and will be cancelled before deletion." not in snap.labels
    assert "Delete" in snap.buttons
    assert "Cancel" in snap.buttons
