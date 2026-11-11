"""Tests for new job page helpers."""

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from nicegui.elements.upload_files import SmallFileUpload
from nicegui.events import UploadEventArguments
from nicegui.testing import user_simulation
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Job, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.new_job import _build_upload_session_id, _submit_job
from openscientist.webapp_components.utils.session import (
    clear_uploaded_files,
    get_uploaded_files,
    upload_storage_root,
)
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.db_outage import Forwarder
from tests.webapp.conftest import OpenBrowser, OpenPage, fire_click_twice


@pytest_asyncio.fixture
async def signed_in_job_user(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, str]]:
    """An approved non-admin with a live session, committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        user = User(email=f"submit-{uuid4().hex}@page.test", name="Submitter", is_approved=True)
        db.add(user)
        await db.flush()
        login = Session(user_id=user.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield user, str(login.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.owner_id == user.id))
        await db.execute(delete(User).where(User.id == user.id))
        await db.commit()


def _upload_event(name: str, size_bytes: int, client: Client) -> UploadEventArguments:
    with client:
        upload = ui.upload()
    return UploadEventArguments(
        sender=upload,
        client=client,
        file=SmallFileUpload(name, "text/csv", b"x" * size_bytes),
    )


def test_build_upload_session_id_uses_user_and_client_id():
    """Upload session IDs should be scoped by user and websocket client."""
    client = SimpleNamespace(id="client-abc")
    session_id = _build_upload_session_id("user-123", client)
    assert session_id == "user-123:client-abc"


def test_build_upload_session_id_handles_missing_user_with_anonymous_prefix():
    """Anonymous fallback should still include client identity."""
    client = SimpleNamespace(id="client-xyz")
    session_id = _build_upload_session_id(None, client)
    assert session_id == "anonymous:client-xyz"


def test_submit_job_has_use_hypotheses_parameter():
    """_submit_job must accept use_hypotheses so the form toggle is wired in."""
    sig = inspect.signature(_submit_job)
    assert "use_hypotheses" in sig.parameters


def test_submit_job_has_coinvestigate_mode_at_top_level():
    """coinvestigate_mode must be a top-level parameter of _submit_job."""
    sig = inspect.signature(_submit_job)
    assert "coinvestigate_mode" in sig.parameters


class _RecordingJobManager:
    """A job manager that records its create_job calls without creating jobs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_job(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_submit_job_creates_the_job_and_opens_its_page(
    database: Forwarder,
    signed_in_job_user: tuple[User, str],
    signed_in_page: OpenPage,
) -> None:
    """Submitting a question creates the job for the user the session still
    belongs to and navigates them to the job's page."""
    user, token = signed_in_job_user
    recorder = _RecordingJobManager()

    class _Form:
        question: ui.textarea
        max_iterations: ui.number
        use_hypotheses: ui.switch
        coinvestigate_mode: ui.switch

    form = _Form()

    async def build() -> None:
        question = ui.textarea()
        question.value = "  Why do corals bleach?  "
        form.question = question
        form.max_iterations = ui.number(value=12)
        form.use_hypotheses = ui.switch(value=True)
        form.coinvestigate_mode = ui.switch(value=False)

    client = await signed_in_page(token, build)
    with client:
        await _submit_job(
            job_manager=recorder,
            session_id=f"{user.id}:success-test",
            research_question=form.question,
            max_iterations=form.max_iterations,
            use_hypotheses=form.use_hypotheses,
            coinvestigate_mode=form.coinvestigate_mode,
        )

    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["research_question"] == "Why do corals bleach?"
    assert call["data_files"] == []
    assert call["max_iterations"] == 12
    assert call["use_hypotheses"] is True
    assert call["auto_start"] is True
    assert call["investigation_mode"] == "autonomous"
    assert call["owner_id"] == str(user.id)
    opened = [data for _target, kind, data in client.outbox.messages if kind == "open"]
    assert opened == [{"path": f"/job/{call['job_id']}", "new_tab": False}]


@pytest.mark.asyncio
async def test_a_double_click_submits_exactly_one_job(
    database: Forwarder,
    test_engine: AsyncEngine,
    signed_in_job_user: tuple[User, str],
    signed_in_page: OpenPage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clicks racing on the event loop create exactly one job, the way
    a double-clicked Start Discovery button would in a browser."""
    from openscientist import web_app
    from openscientist.job_manager import JobManager
    from openscientist.webapp_components.pages import new_job

    user, token = signed_in_job_user
    manager = JobManager(jobs_dir=tmp_path)
    monkeypatch.setattr(manager, "_run_job", lambda job_id, run_mode="discovery": None)
    monkeypatch.setattr(web_app, "get_job_manager", lambda: manager)
    monkeypatch.setattr(new_job, "check_provider_config", lambda: (True, "Mock", None))

    client = await signed_in_page(token, new_job.new_job_page)
    question = next(e for e in client.layout.descendants() if isinstance(e, ui.textarea))
    question.value = "Why do corals bleach when the water warms?"
    button = next(
        e
        for e in client.layout.descendants()
        if isinstance(e, ui.button) and e.text == "Start Discovery"
    )

    await fire_click_twice(button, client, monkeypatch)

    async with AsyncSession(test_engine) as db:
        rows = (await db.execute(select(Job).where(Job.owner_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == JobStatus.PENDING.value
    opened = [data for _target, kind, data in client.outbox.messages if kind == "open"]
    assert opened == [{"path": f"/job/{rows[0].id}", "new_tab": False}]


async def test_an_upload_over_the_rows_size_limit_is_rejected(
    test_engine: AsyncEngine,
    browser_client: OpenBrowser,
) -> None:
    """An upload over the row's max_file_size_mb is not recorded, is not saved,
    and the user is told the limit."""
    from openscientist.webapp_components.pages import new_job

    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    session_id = "limit-reject:client-1"
    event = _upload_event("big.csv", 2 * 1024 * 1024, await browser_client(None))
    with patch.object(new_job, "ui") as mock_ui:
        await new_job._handle_upload(event, session_id)
    assert get_uploaded_files(session_id) == []
    assert not upload_storage_root().exists()
    assert mock_ui.notify.call_args.args[0] == "big.csv exceeds the maximum file size of 1 MB"
    assert mock_ui.notify.call_args.kwargs["type"] == "negative"


async def test_an_upload_under_the_rows_size_limit_is_recorded(
    test_engine: AsyncEngine,
    browser_client: OpenBrowser,
) -> None:
    """An upload under the row's limit is saved and recorded for the job."""
    from openscientist.webapp_components.pages import new_job

    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    session_id = "limit-accept:client-1"
    event = _upload_event("small.csv", 1024, await browser_client(None))
    with patch.object(new_job, "ui") as mock_ui:
        await new_job._handle_upload(event, session_id)
    try:
        files = get_uploaded_files(session_id)
        assert [file["name"] for file in files] == ["small.csv"]
        assert files[0]["path"].read_bytes() == b"x" * 1024
        assert mock_ui.notify.call_args.kwargs["type"] == "positive"
    finally:
        clear_uploaded_files(session_id)


async def test_the_page_shows_the_rows_file_size_limit(
    test_engine: AsyncEngine,
    database: Forwarder,
    signed_in_job_user: tuple[User, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uploader displays and enforces the admin row's per-file limit."""
    from openscientist import web_app
    from openscientist.webapp_components.pages import new_job

    await set_admin_settings_row(test_engine, max_file_size_mb=250)
    monkeypatch.setattr(web_app, "get_job_manager", lambda: object())
    monkeypatch.setattr(new_job, "check_provider_config", lambda: (True, "Mock", None))
    _, token = signed_in_job_user

    async def root() -> None:
        await new_job.new_job_page()

    try:
        async with user_simulation(root=root) as browser:
            browser.http_client.cookies.set("session_token", token)
            await browser.open("/")
            upload = next(iter(browser.find(ui.upload).elements))
            assert upload.props["label"] == "Upload Data Files (Optional - Max 250 MB per file)"
            assert upload.props["max-file-size"] == 250 * 1024 * 1024
            assert "max-total-size" not in upload.props
            assert upload.props["max-files"] == 100
            browser.find(ui.upload).trigger("rejected")
            await browser.should_see("up to 250 MB per file")
            assert len(browser.find(ui.textarea).elements) == 1
    finally:
        await reset_admin_settings_row(test_engine)
