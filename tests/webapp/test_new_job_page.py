"""Tests for new job page helpers."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.webapp_components.pages.new_job import _build_upload_session_id, _submit_job
from openscientist.webapp_components.utils.session import clear_uploaded_files, get_uploaded_files
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row


def _upload_event(name: str, size_bytes: int) -> SimpleNamespace:
    return SimpleNamespace(
        file=SimpleNamespace(
            name=name,
            save=AsyncMock(),
            size=lambda: size_bytes,
        )
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


async def test_submit_job_creates_the_job_and_opens_its_page() -> None:
    """Submitting a question creates the job for the signed-in user and
    navigates them to the job's page."""
    from openscientist.webapp_components.pages import new_job

    job_manager = SimpleNamespace(create_job=AsyncMock())
    form = SimpleNamespace(
        research_question=SimpleNamespace(value="  Why do corals bleach?  "),
        max_iterations=SimpleNamespace(value=12),
        use_hypotheses=SimpleNamespace(value=True),
        coinvestigate_mode=SimpleNamespace(value=False),
    )
    with (
        patch.object(new_job, "ui") as mock_ui,
        patch.object(new_job, "get_current_user_id", return_value="user-123"),
        patch.object(new_job, "get_uploaded_files", return_value=[]),
        patch.object(new_job, "uuid") as mock_uuid,
    ):
        mock_uuid.uuid4.return_value = "00000000-1111-2222-3333-444444444444"
        await new_job._submit_job(
            job_manager=job_manager,
            user_can_start_jobs=True,
            session_id="user-123:client-1",
            research_question=form.research_question,
            max_iterations=form.max_iterations,
            use_hypotheses=form.use_hypotheses,
            coinvestigate_mode=form.coinvestigate_mode,
        )

    job_manager.create_job.assert_awaited_once()
    assert job_manager.create_job.await_args.kwargs == {
        "job_id": "00000000-1111-2222-3333-444444444444",
        "research_question": "Why do corals bleach?",
        "data_files": [],
        "max_iterations": 12,
        "use_hypotheses": True,
        "auto_start": True,
        "investigation_mode": "autonomous",
        "owner_id": "user-123",
    }
    mock_ui.navigate.to.assert_called_with("/job/00000000-1111-2222-3333-444444444444")


async def test_an_upload_over_the_rows_size_limit_is_rejected(
    test_engine: AsyncEngine,
) -> None:
    """An upload over the row's max_file_size_mb is not recorded, is not saved,
    and the user is told the limit."""
    from openscientist.webapp_components.pages import new_job

    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    session_id = "limit-reject:client-1"
    event = _upload_event("big.csv", 2 * 1024 * 1024)
    with patch.object(new_job, "ui") as mock_ui:
        await new_job._handle_upload(event, session_id)
    assert get_uploaded_files(session_id) == []
    event.file.save.assert_not_awaited()
    assert mock_ui.notify.call_args.args[0] == "big.csv exceeds the maximum file size of 1 MB"
    assert mock_ui.notify.call_args.kwargs["type"] == "negative"


async def test_an_upload_under_the_rows_size_limit_is_recorded(
    test_engine: AsyncEngine,
) -> None:
    """An upload under the row's limit is saved and recorded for the job."""
    from openscientist.webapp_components.pages import new_job

    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    session_id = "limit-accept:client-1"
    event = _upload_event("small.csv", 1024)
    with patch.object(new_job, "ui") as mock_ui:
        await new_job._handle_upload(event, session_id)
    try:
        files = get_uploaded_files(session_id)
        assert [file["name"] for file in files] == ["small.csv"]
        event.file.save.assert_awaited_once()
        assert mock_ui.notify.call_args.kwargs["type"] == "positive"
    finally:
        clear_uploaded_files(session_id)


async def test_the_page_shows_the_rows_file_size_limit(
    test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signed-in page shows the limit the admin settings row holds in the upload
    header, and the research-question field."""
    from openscientist import web_app
    from openscientist.auth import middleware
    from openscientist.webapp_components.pages import new_job

    await set_admin_settings_row(test_engine, max_file_size_mb=250)
    try:
        storage: dict[str, object] = {}
        ui = MagicMock()
        ui.context.client.request.cookies = {"session_token": "page-token"}
        ui.context.client.page.path = "/new"
        ui.context.client.page.response_timeout = 30.0
        monkeypatch.setattr(
            middleware, "app", SimpleNamespace(storage=SimpleNamespace(user=storage))
        )
        monkeypatch.setattr(middleware, "ui", ui)

        async def _valid(_token: str) -> dict[str, object]:
            return {
                "user_id": "user-123",
                "email": "visitor@example.com",
                "name": "Visitor",
                "can_start_jobs": True,
            }

        monkeypatch.setattr(middleware, "validate_session", _valid)
        mock_ui = MagicMock()
        with (
            patch.object(new_job, "ui", mock_ui),
            patch.object(new_job, "render_navigator", MagicMock()),
            patch.object(new_job, "check_provider_config", lambda: (True, "Anthropic", None)),
            patch.object(web_app, "get_job_manager", MagicMock()),
        ):
            await new_job.new_job_page()

        assert mock_ui.upload.call_args.kwargs["label"] == (
            "Upload Data Files (Optional - Max 250 MB per file)"
        )
        mock_ui.textarea.assert_called_once()
        assert mock_ui.textarea.call_args.kwargs["label"] == "Research Question"
    finally:
        await reset_admin_settings_row(test_engine)
