"""The /new upload session leaves no trace once the page is gone.

Each /new load keys its uploaded files by the user and the page load's
websocket client. The page's disconnect handler clears the entry for a
visitor who leaves without submitting, and a submit clears it for one who
starts a job, so neither path may leave an entry in the process-global dict.

The page is the real /new page built behind the login cookie in a real
NiceGUI client, and the upload and the submit fire through the handlers the
page registered, the way NiceGUI's upload route and socket handler do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, storage, ui
from nicegui.client import Client
from nicegui.elements.upload_files import FileUpload, create_file_upload
from nicegui.events import UploadEventArguments, handle_event
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from openscientist import web_app
from openscientist.database.models import Session, User
from openscientist.webapp_components.pages import new_job
from openscientist.webapp_components.utils.session import _uploaded_files
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenPage, in_event_context


@pytest_asyncio.fixture
async def new_page_user(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, str]]:
    """An approved non-admin with a live session, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"upload-{uuid4().hex}@page.test", name="Uploader", is_approved=True)
        db.add(user)
        await db.flush()
        login, login_token = Session.issue(
            user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(login)
        await db.commit()
    yield user, login_token
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


class _RecordingJobManager:
    """A job manager that records its create_job calls without creating jobs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_job(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


async def _file_upload(name: str, data: bytes) -> FileUpload:
    """A NiceGUI file upload carrying ``data``, the way the upload route builds it."""
    return await create_file_upload(
        StarletteUploadFile(
            file=BytesIO(data), filename=name, headers=Headers({"content-type": "text/csv"})
        )
    )


async def _open_new_page(
    signed_in_page: OpenPage, token: str, job_manager: object, monkeypatch: pytest.MonkeyPatch
) -> Client:
    """Build the real /new page behind the login cookie and return its client."""
    monkeypatch.setattr(web_app, "get_job_manager", lambda: job_manager)
    monkeypatch.setattr(new_job, "check_provider_config", lambda: (True, "Mock", None))
    return await signed_in_page(token, new_job.new_job_page)


async def _fire_upload(
    upload: ui.upload, client: Client, file: FileUpload, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fire the page's upload handler the way NiceGUI's upload route does and
    await it.

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

    storage.request_contextvar.set(client.request)
    with monkeypatch.context() as capture:
        capture.setattr(background_tasks, "create_or_defer", _capture)
        with client:
            with upload.parent_slot or nullcontext():
                handle_event(
                    upload._upload_handlers[0],
                    UploadEventArguments(sender=upload, client=client, file=file),
                )
    assert len(scheduled) == 1
    await scheduled[0]


def _disconnect(client: Client) -> None:
    """Run the client's disconnect handlers the way a closed websocket does.

    NiceGUI's handle_disconnect fires each page-level handler through
    safe_invoke and then schedules the client's element removal, which needs
    the running app's loop and is not under test here."""
    for handler in client.disconnect_handlers:
        client.safe_invoke(handler)


async def _fire_click(button: ui.button, client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire the button's click the way NiceGUI's socket handler does and await
    it.

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
    assert len(scheduled) == 1
    await scheduled[0]


@pytest.mark.asyncio
async def test_a_new_page_load_without_uploads_leaves_no_entry(
    database: Forwarder,
    new_page_user: tuple[User, str],
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated /new loads that never upload leave no upload-session entries."""
    user, token = new_page_user
    for _ in range(3):
        client = await _open_new_page(signed_in_page, token, object(), monkeypatch)
        assert len(client.disconnect_handlers) == 1
        _disconnect(client)
    assert not any(key.startswith(f"{user.id}:") for key in _uploaded_files)


@pytest.mark.asyncio
async def test_a_new_page_load_with_uploads_leaves_no_entry(
    database: Forwarder,
    new_page_user: tuple[User, str],
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A /new load that uploads files and leaves without submitting removes its
    entry and its temp files, and repeated loads add no more."""
    user, token = new_page_user
    for index in range(3):
        client = await _open_new_page(signed_in_page, token, object(), monkeypatch)
        before = set(_uploaded_files)
        upload = next(e for e in client.layout.descendants() if isinstance(e, ui.upload))
        file = await _file_upload(f"upload-{index}.csv", b"col1,col2\n1,2\n")
        await _fire_upload(upload, client, file, monkeypatch)
        new_keys = set(_uploaded_files) - before
        assert len(new_keys) == 1
        session_id = next(iter(new_keys))
        recorded = _uploaded_files[session_id]
        assert [f["name"] for f in recorded] == [f"upload-{index}.csv"]
        _disconnect(client)
        assert session_id not in _uploaded_files
        for uploaded_file in recorded:
            assert not uploaded_file["path"].exists()
    assert not any(key.startswith(f"{user.id}:") for key in _uploaded_files)


@pytest.mark.asyncio
async def test_a_submit_still_sees_the_uploaded_files_and_leaves_no_entry(
    database: Forwarder,
    new_page_user: tuple[User, str],
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Submitting with uploaded files hands them to create_job and leaves no
    upload-session entry behind."""
    user, token = new_page_user
    recorder = _RecordingJobManager()
    client = await _open_new_page(signed_in_page, token, recorder, monkeypatch)
    before = set(_uploaded_files)
    upload = next(e for e in client.layout.descendants() if isinstance(e, ui.upload))
    file = await _file_upload("sample.csv", b"col1,col2\n1,2\n")
    await _fire_upload(upload, client, file, monkeypatch)
    new_keys = set(_uploaded_files) - before
    assert len(new_keys) == 1
    session_id = next(iter(new_keys))
    recorded = _uploaded_files[session_id]
    assert len(recorded) == 1
    temp_path: Path = recorded[0]["path"]
    assert temp_path.exists()

    question = next(e for e in client.layout.descendants() if isinstance(e, ui.textarea))
    question.value = "Why do corals bleach when the water warms?"
    button = next(
        e
        for e in client.layout.descendants()
        if isinstance(e, ui.button) and e.text == "Start Discovery"
    )
    await _fire_click(button, client, monkeypatch)

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["data_files"] == [temp_path]
    assert call["owner_id"] == str(user.id)
    assert session_id not in _uploaded_files
    assert not temp_path.exists()
    opened = [data for _target, kind, data in client.outbox.messages if kind == "open"]
    assert opened == [{"path": f"/job/{call['job_id']}", "new_tab": False}]
