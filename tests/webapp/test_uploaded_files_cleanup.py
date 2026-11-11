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
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from nicegui import app, background_tasks, storage, ui
from nicegui.client import Client
from nicegui.elements.upload_files import FileUpload, SmallFileUpload, create_file_upload
from nicegui.events import UploadEventArguments, handle_event
from nicegui.persistence.file_persistent_dict import FilePersistentDict
from nicegui.testing import user_simulation
from nicegui.testing.user import User as SimulatedBrowser
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette import formparsers
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from openscientist import web_app
from openscientist.admin_settings import AdminSettingsValues
from openscientist.database.models import Job, Session, User
from openscientist.job_manager import JobManager
from openscientist.upload_limits import UploadLimitsMiddleware
from openscientist.webapp_components.pages import new_job
from openscientist.webapp_components.utils.session import (
    _uploaded_files,
    clear_upload_storage,
)
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
    root = Path(os.environ["NICEGUI_STORAGE_PATH"]) / "uploads"
    stale = root / "file-abandoned" / "data.csv"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"a,b\n1,2\n")
    clear_upload_storage()
    assert not root.exists()


@asynccontextmanager
async def _upload_removal_page(
    user: User,
    token: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[SimulatedBrowser, ui.upload, str]]:
    manager = JobManager(jobs_dir=tmp_path / "jobs")
    monkeypatch.setattr(manager, "_run_job", lambda job_id, run_mode="discovery": None)
    monkeypatch.setattr(web_app, "get_job_manager", lambda: manager)
    monkeypatch.setattr(new_job, "check_provider_config", lambda: (True, "Mock", None))
    monkeypatch.setattr(new_job, "MAX_UPLOAD_FILES_PER_REQUEST", 2)

    async def root() -> None:
        await new_job.new_job_page()

    async with user_simulation(root=root) as browser:
        monkeypatch.setattr(
            browser.http_client, "_transport", httpx.ASGITransport(UploadLimitsMiddleware(app))
        )
        browser.http_client.cookies.set("session_token", token)
        client = await browser.open("/")
        monkeypatch.setattr(ui.navigate, "to", lambda *_args, **_kwargs: None)
        upload = next(iter(browser.find(ui.upload).elements))
        session_id = new_job._build_upload_session_id(str(user.id), client)
        try:
            yield browser, upload, session_id
        finally:
            clear_upload_storage()


async def _post_upload(
    browser: SimulatedBrowser, upload: ui.upload, name: str, data: bytes
) -> None:
    browser.notify.messages.clear()
    response = await asyncio.wait_for(
        browser.http_client.post(upload.props["url"], files={"file": (name, data, "text/csv")}),
        timeout=10,
    )
    assert response.status_code == 200
    await browser.should_see("Uploaded:", retries=50)


@pytest.mark.parametrize(
    ("first_name", "first_data", "second_name", "second_data"),
    [
        ("kept.csv", b"keep\n", "removed.csv", b"remove\n"),
        ("same.csv", b"keep\n", "same.csv", b"remove\n"),
        ("same.csv", b"first\n", "same.csv", b"other\n"),
        ("same.csv", b"keep\n", r"C:\fakepath\same.csv", b"remove\n"),
    ],
    ids=["distinct", "duplicate-name", "identical-name-and-size", "sanitized-name"],
)
async def test_removed_upload_is_not_in_the_created_job_and_releases_quota(
    test_engine: AsyncEngine,
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_name: str,
    first_data: bytes,
    second_name: str,
    second_data: bytes,
) -> None:
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        await _post_upload(browser, upload, first_name, first_data)
        if second_name.rsplit("\\", 1)[-1] == first_name:
            response = await asyncio.wait_for(
                browser.http_client.post(
                    upload.props["url"], files={"file": (second_name, second_data, "text/csv")}
                ),
                timeout=10,
            )
            assert response.status_code == 409
            browser.find(ui.upload).trigger(
                "removed", [{"name": second_name, "__status": "failed"}]
            )
        else:
            await _post_upload(browser, upload, second_name, second_data)
            removed_path = _uploaded_files[session_id][-1]["path"]
            browser.find(ui.upload).trigger(
                "removed", [{"name": second_name, "__status": "uploaded"}]
            )
            assert not removed_path.exists()
        assert [entry["path"].read_bytes() for entry in _uploaded_files[session_id]] == [first_data]

        await _post_upload(browser, upload, "replacement.csv", b"quota\n")
        assert len(_uploaded_files[session_id]) == 2
        browser.find(ui.upload).trigger(
            "removed", [{"name": "replacement.csv", "__status": "uploaded"}]
        )
        browser.find(ui.textarea).type("Why does the uploaded measurement vary?")
        browser.find("Start Discovery").click()
        await browser.should_see("created and started!", retries=100)

        async with AsyncSession(test_engine) as db:
            job = (await db.execute(select(Job).where(Job.owner_id == user.id))).scalar_one()
        data_dir = tmp_path / "jobs" / str(job.id) / "data"
        assert [path.name for path in data_dir.iterdir()] == [first_name]
        assert (data_dir / first_name).read_bytes() == first_data


@pytest.mark.parametrize("action", ["batch-removed", "reset"])
async def test_clearing_the_upload_list_deletes_all_staged_files(
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        await _post_upload(browser, upload, "first.csv", b"first\n")
        await _post_upload(browser, upload, "second.csv", b"second\n")
        paths = [entry["path"] for entry in _uploaded_files[session_id]]
        if action == "reset":
            upload.reset()
        else:
            browser.find(ui.upload).trigger(
                "removed",
                [
                    {"name": "first.csv", "__status": "uploaded"},
                    {"name": "second.csv", "__status": "uploaded"},
                ],
            )
        assert not _uploaded_files.get(session_id)
        assert not any(path.exists() for path in paths)
        await _post_upload(browser, upload, "replacement.csv", b"new\n")
        assert len(_uploaded_files[session_id]) == 1


@pytest.mark.parametrize("action", ["removed", "reset"])
async def test_removing_an_upload_during_save_does_not_restore_it(
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        started = asyncio.Event()
        resume = asyncio.Event()
        completed = asyncio.Event()
        paths: list[Path] = []

        async def paused_save(file: SmallFileUpload, path: str | Path) -> None:
            paths.append(Path(path))
            Path(path).write_bytes(b"partial")
            started.set()
            await asyncio.wait_for(resume.wait(), timeout=5)
            Path(path).write_bytes(await file.read())
            completed.set()

        monkeypatch.setattr(SmallFileUpload, "save", paused_save)
        response = await asyncio.wait_for(
            browser.http_client.post(
                upload.props["url"], files={"file": ("pending.csv", b"a,b\n1,2\n", "text/csv")}
            ),
            timeout=10,
        )
        assert response.status_code == 200
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            duplicate = await asyncio.wait_for(
                browser.http_client.post(
                    upload.props["url"], files={"file": ("pending.csv", b"different", "text/csv")}
                ),
                timeout=10,
            )
            assert duplicate.status_code == 409
            browser.find(ui.upload).trigger(
                "removed", [{"name": "pending.csv", "__status": "failed"}]
            )
            if action == "reset":
                upload.reset()
            else:
                browser.find(ui.upload).trigger(
                    "removed", [{"name": "pending.csv", "__status": "uploaded"}]
                )
        finally:
            resume.set()
            await asyncio.wait_for(completed.wait(), timeout=5)
        assert not _uploaded_files.get(session_id)
        assert paths and not any(path.exists() for path in paths)


async def test_removal_before_settings_return_cancels_the_pending_upload(
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        admin = await new_job.current_admin_settings()
        started = asyncio.Event()
        resume = asyncio.Event()
        completed = asyncio.Event()

        async def delayed_settings() -> AdminSettingsValues:
            started.set()
            await asyncio.wait_for(resume.wait(), timeout=5)
            completed.set()
            return admin

        monkeypatch.setattr(new_job, "current_admin_settings", delayed_settings)
        response = await asyncio.wait_for(
            browser.http_client.post(
                upload.props["url"], files={"file": ("pending.csv", b"pending\n", "text/csv")}
            ),
            timeout=10,
        )
        assert response.status_code == 200
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            browser.find(ui.upload).trigger(
                "removed", [{"name": "pending.csv", "__status": "uploaded"}]
            )
        finally:
            resume.set()
            await asyncio.wait_for(completed.wait(), timeout=5)
        assert not _uploaded_files.get(session_id)
        assert not (Path(os.environ["NICEGUI_STORAGE_PATH"]) / "uploads").exists()


async def test_removed_event_only_selects_matching_session_files(
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        await _post_upload(browser, upload, "kept.csv", b"keep\n")
        staged_path = _uploaded_files[session_id][0]["path"]
        outside = tmp_path / "outside.csv"
        outside.write_bytes(b"other\n")
        browser.find(ui.upload).trigger(
            "removed",
            [
                {"name": "kept.csv", "__status": "failed", "path": str(staged_path)},
                {"name": "../outside.csv", "__status": "uploaded", "path": str(outside)},
                {"name": "kept.csv", "__status": "uploading"},
                {"name": "kept.csv"},
                "kept.csv",
            ],
        )
        assert staged_path.exists()
        assert outside.exists()
        browser.find(ui.upload).trigger(
            "removed", [{"name": "kept.csv", "__status": "uploaded", "path": str(outside)}]
        )
        assert not staged_path.exists()
        assert outside.read_bytes() == b"other\n"


@pytest.mark.parametrize("duplicate_name", ["same.csv", r"C:\fakepath\same.csv", "folder/same.csv"])
async def test_duplicate_upload_rejection_preserves_the_uploaded_file(
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duplicate_name: str,
) -> None:
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        await _post_upload(browser, upload, "same.csv", b"first\n")
        stored = _uploaded_files[session_id][0]["path"]
        with patch.object(
            formparsers, "SpooledTemporaryFile", wraps=formparsers.SpooledTemporaryFile
        ) as spool:
            response = await asyncio.wait_for(
                browser.http_client.post(
                    upload.props["url"], files={"file": (duplicate_name, b"other\n", "text/csv")}
                ),
                timeout=10,
            )
        spool.assert_not_called()
        assert response.status_code == 409
        browser.find(ui.upload).trigger("removed", [{"name": duplicate_name, "__status": "failed"}])
        assert stored.read_bytes() == b"first\n"
        assert len(_uploaded_files[session_id]) == 1
        browser.find(ui.upload).trigger("removed", [{"name": "same.csv", "__status": "uploaded"}])
        assert not stored.exists()
        assert not _uploaded_files.get(session_id)
        await _post_upload(browser, upload, "same.csv", b"again\n")


async def test_duplicate_name_is_reserved_while_receiving(
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        started = asyncio.Event()
        resume = asyncio.Event()

        async def body() -> AsyncIterator[bytes]:
            yield (
                b'--upload\r\nContent-Disposition: form-data; name="file"; '
                b'filename="same.csv"\r\nContent-Type: text/csv\r\n\r\n'
            )
            started.set()
            await asyncio.wait_for(resume.wait(), timeout=5)
            yield b"first\n\r\n--upload--\r\n"

        request = asyncio.create_task(
            browser.http_client.post(
                upload.props["url"],
                content=body(),
                headers={"content-type": "multipart/form-data; boundary=upload"},
            ),
            name="receiving-same.csv",
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            duplicate = await asyncio.wait_for(
                browser.http_client.post(
                    upload.props["url"], files={"file": ("same.csv", b"other\n", "text/csv")}
                ),
                timeout=10,
            )
            assert duplicate.status_code == 409
            browser.find(ui.upload).trigger("removed", [{"name": "same.csv", "__status": "failed"}])
        finally:
            resume.set()
            response = await asyncio.wait_for(request, timeout=10)
        assert response.status_code == 200
        await browser.should_see("Uploaded:", retries=50)
        assert [entry["path"].read_bytes() for entry in _uploaded_files[session_id]] == [b"first\n"]


@pytest.mark.parametrize("action", ["removed", "reset"])
async def test_failed_save_keeps_its_name_until_the_entry_is_removed(
    test_engine: AsyncEngine,
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=2)
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        admin = await new_job.current_admin_settings()

        async def reduced_limit() -> AdminSettingsValues:
            return replace(admin, max_file_size_mb=1)

        monkeypatch.setattr(new_job, "current_admin_settings", reduced_limit)
        response = await asyncio.wait_for(
            browser.http_client.post(
                upload.props["url"],
                files={"file": ("same.csv", b"x" * (1024 * 1024 + 1), "text/csv")},
            ),
            timeout=10,
        )
        assert response.status_code == 200
        await browser.should_see("exceeds the maximum file size", retries=50)
        assert not _uploaded_files.get(session_id)
        duplicate = await asyncio.wait_for(
            browser.http_client.post(
                upload.props["url"], files={"file": ("same.csv", b"other\n", "text/csv")}
            ),
            timeout=10,
        )
        assert duplicate.status_code == 409
        untouched = tmp_path / "same.csv"
        untouched.write_bytes(b"untouched")
        if action == "reset":
            upload.reset()
        else:
            browser.find(ui.upload).trigger(
                "removed", [{"name": "same.csv", "__status": "uploaded"}]
            )
        assert untouched.read_bytes() == b"untouched"
        assert not _uploaded_files.get(session_id)
        await _post_upload(browser, upload, "same.csv", b"accepted\n")
        assert _uploaded_files[session_id][0]["path"].read_bytes() == b"accepted\n"


async def test_page_quota_rejection_releases_the_unclaimed_name(
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        await _post_upload(browser, upload, "first.csv", b"first\n")
        await _post_upload(browser, upload, "second.csv", b"second\n")
        with patch.object(
            formparsers, "SpooledTemporaryFile", wraps=formparsers.SpooledTemporaryFile
        ) as spool:
            response = await asyncio.wait_for(
                browser.http_client.post(
                    upload.props["url"], files={"file": ("third.csv", b"third\n", "text/csv")}
                ),
                timeout=10,
            )
        spool.assert_not_called()
        assert response.status_code == 413
        browser.find(ui.upload).trigger("removed", [{"name": "first.csv", "__status": "uploaded"}])
        await _post_upload(browser, upload, "third.csv", b"third\n")
        assert {entry["name"] for entry in _uploaded_files[session_id]} == {
            "second.csv",
            "third.csv",
        }


async def test_page_byte_quota_uses_the_current_limit(
    test_engine: AsyncEngine,
    database: Forwarder,
    new_page_user: tuple[User, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=2)
    user, token = new_page_user
    async with _upload_removal_page(user, token, tmp_path, monkeypatch) as (
        browser,
        upload,
        session_id,
    ):
        await _post_upload(browser, upload, "large.csv", b"x" * (2 * 1024 * 1024))
        await set_admin_settings_row(test_engine, max_file_size_mb=1)
        response = await asyncio.wait_for(
            browser.http_client.post(
                upload.props["url"], files={"file": ("extra.csv", b"extra\n", "text/csv")}
            ),
            timeout=10,
        )
        assert response.status_code == 413
        assert [entry["name"] for entry in _uploaded_files[session_id]] == ["large.csv"]
