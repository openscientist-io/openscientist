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

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import app, background_tasks, storage, ui
from nicegui.client import Client
from nicegui.elements.upload_files import FileUpload, create_file_upload
from nicegui.events import UploadEventArguments, handle_event
from nicegui.persistence.file_persistent_dict import FilePersistentDict
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from openscientist import web_app
from openscientist.database.models import Session, User
from openscientist.webapp_components.pages import new_job
from openscientist.webapp_components.utils.session import _uploaded_files
from tests.admin_row import set_admin_settings_row
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenPage


@pytest_asyncio.fixture
async def new_page_user(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, str]]:
    """An approved non-admin with a live session, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"upload-{uuid4().hex}@page.test", name="Uploader", is_approved=True)
        db.add(user)
        await db.flush()
        login = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield user, str(login.id)
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

    def _capture(awaitable: Awaitable[None], *, name: str = "") -> None:
        scheduled.append(awaitable)

    monkeypatch.setattr(background_tasks, "create_or_defer", _capture)
    storage.request_contextvar.set(client.request)
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

    def _capture(awaitable: Awaitable[None], *, name: str = "") -> None:
        scheduled.append(awaitable)

    monkeypatch.setattr(background_tasks, "create_or_defer", _capture)
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    handler = listener.handler
    storage.request_contextvar.set(listener.request)
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


@pytest.mark.parametrize("concurrent", [False, True])
async def test_page_session_refuses_files_past_its_cap_without_leaving_temp_files(
    test_engine: AsyncEngine,
    database: Forwarder,
    new_page_user: tuple[User, str],
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
    concurrent: bool,
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    monkeypatch.setattr(new_job, "MAX_UPLOAD_FILES_PER_REQUEST", 2)
    user, token = new_page_user
    client = await _open_new_page(signed_in_page, token, object(), monkeypatch)
    upload = next(e for e in client.layout.descendants() if isinstance(e, ui.upload))
    messages: list[str] = []
    monkeypatch.setattr(new_job.ui, "notify", lambda message, **kwargs: messages.append(message))
    files = [await _file_upload(f"{index}.csv", b"a,b\n1,2\n") for index in range(3)]
    session_id = new_job._build_upload_session_id(str(user.id), client)
    try:
        if concurrent:
            await asyncio.gather(
                *(_fire_upload(upload, client, file, monkeypatch) for file in files)
            )
        else:
            for file in files:
                await _fire_upload(upload, client, file, monkeypatch)
        recorded = _uploaded_files[session_id]
        assert len(recorded) == 2
        assert any("at most 2 files" in message for message in messages)
        root = Path(os.environ["NICEGUI_STORAGE_PATH"]) / "uploads"
        assert {path for path in root.rglob("*") if path.is_file()} == {
            entry["path"] for entry in recorded
        }
        assert all(root in entry["path"].parents for entry in recorded)
    finally:
        _disconnect(client)
    assert list(root.iterdir()) == []


async def test_page_session_total_tracks_the_current_admin_limit(
    test_engine: AsyncEngine,
    database: Forwarder,
    new_page_user: tuple[User, str],
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=2)
    monkeypatch.setattr(new_job, "MAX_UPLOAD_FILES_PER_REQUEST", 2)
    user, token = new_page_user
    client = await _open_new_page(signed_in_page, token, object(), monkeypatch)
    upload = next(e for e in client.layout.descendants() if isinstance(e, ui.upload))
    messages: list[str] = []
    monkeypatch.setattr(new_job.ui, "notify", lambda message, **kwargs: messages.append(message))
    session_id = new_job._build_upload_session_id(str(user.id), client)
    try:
        await _fire_upload(
            upload, client, await _file_upload("first.csv", b"x" * (2 * 1024 * 1024)), monkeypatch
        )
        first_path = _uploaded_files[session_id][0]["path"]
        await set_admin_settings_row(test_engine, max_file_size_mb=1)
        await _fire_upload(upload, client, await _file_upload("second.csv", b"x"), monkeypatch)
        assert len(_uploaded_files[session_id]) == 1
        assert any("total limit of 2 MB" in message for message in messages)
        root = Path(os.environ["NICEGUI_STORAGE_PATH"]) / "uploads"
        assert {path for path in root.rglob("*") if path.is_file()} == {first_path}
    finally:
        _disconnect(client)
    assert list(root.iterdir()) == []


async def test_disconnect_during_upload_removes_the_pending_file(
    database: Forwarder,
    new_page_user: tuple[User, str],
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, token = new_page_user
    client = await _open_new_page(signed_in_page, token, object(), monkeypatch)
    upload = next(e for e in client.layout.descendants() if isinstance(e, ui.upload))
    file = await _file_upload("pending.csv", b"a,b\n1,2\n")
    started = asyncio.Event()
    resume = asyncio.Event()

    async def paused_save(path: str | Path) -> None:
        Path(path).write_bytes(b"a,b\n")
        started.set()
        await asyncio.wait_for(resume.wait(), timeout=5)
        Path(path).write_bytes(b"a,b\n1,2\n")

    monkeypatch.setattr(file, "save", paused_save)
    pending = asyncio.create_task(
        _fire_upload(upload, client, file, monkeypatch), name="pending-upload"
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        _disconnect(client)
    finally:
        resume.set()
        await asyncio.wait_for(pending, timeout=5)
    assert not any(key.startswith(f"{user.id}:") for key in _uploaded_files)
    root = Path(os.environ["NICEGUI_STORAGE_PATH"]) / "uploads"
    assert list(root.iterdir()) == []


async def test_failed_upload_removes_its_partial_file(
    database: Forwarder,
    new_page_user: tuple[User, str],
    signed_in_page: OpenPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, token = new_page_user
    client = await _open_new_page(signed_in_page, token, object(), monkeypatch)
    upload = next(e for e in client.layout.descendants() if isinstance(e, ui.upload))
    file = await _file_upload("failed.csv", b"a,b\n1,2\n")

    async def failed_save(path: str | Path) -> None:
        Path(path).write_bytes(b"a,b\n")
        raise OSError("Upload interrupted")

    monkeypatch.setattr(file, "save", failed_save)
    try:
        await _fire_upload(upload, client, file, monkeypatch)
        root = Path(os.environ["NICEGUI_STORAGE_PATH"]) / "uploads"
        assert list(root.iterdir()) == []
        session_id = new_job._build_upload_session_id(str(user.id), client)
        assert _uploaded_files[session_id] == []
    finally:
        _disconnect(client)


def test_nicegui_storage_uses_the_test_directory(tmp_path: Path) -> None:
    root = tmp_path / ".nicegui"
    assert storage.Storage.path == root
    assert isinstance(app.storage._general, FilePersistentDict)
    assert app.storage._general.filepath == root / "storage-general.json"
    sibling = tmp_path / "keep.txt"
    sibling.write_text("keep")

    app.storage.general["probe"] = True
    assert (root / "storage-general.json").is_file()
    app.storage.clear()

    assert not root.exists()
    assert sibling.read_text() == "keep"


def test_upload_storage_cleanup_removes_crash_leftovers() -> None:
    from openscientist.webapp_components.utils.session import clear_upload_storage

    root = Path(os.environ["NICEGUI_STORAGE_PATH"]) / "uploads"
    stale = root / "file-abandoned" / "data.csv"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"a,b\n1,2\n")
    clear_upload_storage()
    assert not root.exists()
