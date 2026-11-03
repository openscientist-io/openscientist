"""Public job visibility: the API toggle, file routes and the dialog switch.

The owner flips a job public; any signed-in user then reads the job and its
analysis rows but not its uploaded inputs (``data/``), chat or costs. The
owner and share recipients keep everything. Only the owner may change the flag.
"""

from __future__ import annotations

import base64
import contextlib
import io
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.artifact_packager import artifact_file, create_artifacts_zip_file
from openscientist.database.models import Job, JobShare, Session, User
from openscientist.database.rls import set_current_user
from openscientist.job.types import JobInfo, JobStatus
from openscientist.share_service import (
    PUBLIC_VIEWER_EXCLUDED_DIRS,
    set_job_public,
    viewer_has_direct_access,
    withheld_artifact_dirs,
)
from openscientist.webapp_components.components import dialogs as dialog_module
from openscientist.webapp_components.pages.job_detail import _owner_name, timeline
from tests.helpers import enable_rls, fake_admin_session


async def _job(db_session: AsyncSession, owner: User, *, is_public: bool) -> Job:
    job = Job(
        owner_id=owner.id,
        research_question="Public visibility job",
        description="visibility test",
        status="completed",
        is_public=is_public,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


def _manager(jobs_dir: Path):
    class _Manager:
        def __init__(self) -> None:
            self.jobs_dir = jobs_dir

    return _Manager()


def _api_app(db_session: AsyncSession, user: User) -> FastAPI:
    from openscientist.api.auth import get_current_user_from_api_key
    from openscientist.api.router import api_router
    from openscientist.database.session import get_session

    app = FastAPI()

    async def override_get_session():
        await set_current_user(db_session, user.id)
        yield db_session

    async def override_get_user():
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_from_api_key] = override_get_user
    app.include_router(api_router)
    return app


def _web_app() -> FastAPI:
    """The artifact router on the app's real per-request sessions."""
    from openscientist.webapp_components.artifact_routes import router

    app = FastAPI()
    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def committed_public_job(
    test_engine: AsyncEngine,
) -> AsyncIterator[tuple[User, User, Job, str, str]]:
    """A committed public job with its owner and a viewer, each holding a
    live session, so the web's real per-request sessions sign their login
    cookies in the way the login flow does."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(
            email=f"public-owner-{uuid4().hex}@public.test",
            name="Public Owner",
            is_approved=True,
        )
        viewer = User(
            email=f"public-viewer-{uuid4().hex}@public.test",
            name="Public Viewer",
            is_approved=True,
        )
        db.add(owner)
        db.add(viewer)
        await db.flush()
        owner_login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        viewer_login = Session(user_id=viewer.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        job = Job(
            owner_id=owner.id,
            research_question="Public visibility job",
            description="visibility test",
            status="completed",
            is_public=True,
        )
        db.add(owner_login)
        db.add(viewer_login)
        db.add(job)
        await db.commit()
        await db.refresh(job)
    yield owner, viewer, job, str(owner_login.id), str(viewer_login.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, viewer.id])))
        await db.commit()


@pytest.mark.asyncio
async def test_new_jobs_are_private_by_default(db_session: AsyncSession, test_user: User):
    job = Job(owner_id=test_user.id, research_question="Default", status="pending")
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    assert job.is_public is False


@pytest.mark.asyncio
async def test_set_job_public_is_owner_only(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=False)
    await enable_rls(db_session)

    await set_current_user(db_session, test_user.id)
    updated = await set_job_public(
        db_session,
        test_user.id,
        job_id=str(job.id),
        is_public=True,
        not_owned_detail="no",
    )
    assert updated.is_public is True

    await set_current_user(db_session, test_user2.id)
    with pytest.raises(HTTPException) as refused:
        await set_job_public(
            db_session,
            test_user2.id,
            job_id=str(job.id),
            is_public=False,
            not_owned_detail="Only the job owner can change its visibility",
        )
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_viewer_has_direct_access_only_for_owner_and_sharees(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=True)
    db_session.add(
        JobShare(job_id=job.id, shared_with_user_id=test_user2.id, permission_level="view")
    )
    await db_session.commit()
    await enable_rls(db_session)

    await set_current_user(db_session, test_user.id)
    assert await viewer_has_direct_access(db_session, job, test_user.id) is True
    await set_current_user(db_session, test_user2.id)
    assert await viewer_has_direct_access(db_session, job, test_user2.id) is True


@pytest.mark.asyncio
async def test_withheld_dirs_hide_uploads_only_from_public_viewers(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)

    await set_current_user(db_session, test_user.id)
    assert await withheld_artifact_dirs(db_session, job, test_user.id) == ()
    await set_current_user(db_session, test_user2.id)
    assert (
        await withheld_artifact_dirs(db_session, job, test_user2.id) == PUBLIC_VIEWER_EXCLUDED_DIRS
    )


def test_packager_leaves_out_uploads(tmp_path: Path):
    job_dir = tmp_path / "job"
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "provenance").mkdir()
    (job_dir / "provenance" / "data").mkdir(parents=True)
    (job_dir / "provenance" / "data" / "derived.csv").write_text("x\n")
    (job_dir / "report.md").write_text("# Report")
    archive = tmp_path / "out.zip"

    create_artifacts_zip_file(job_dir, archive, "job", excluded_top_level_dirs=("data",))

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "report.md" in names
    assert "provenance/data/derived.csv" in names
    assert not any(name.startswith("data/") for name in names)


def test_artifact_file_withholds_uploads(tmp_path: Path):
    job_dir = tmp_path / "job"
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "report.md").write_text("# Report")

    assert artifact_file(job_dir, "report.md") is not None
    assert artifact_file(job_dir, "data/cohort.csv") is not None
    # A public reader gets no file under the withheld top-level directory.
    assert artifact_file(job_dir, "data/cohort.csv", excluded_top_level_dirs=("data",)) is None
    assert artifact_file(job_dir, "report.md", excluded_top_level_dirs=("data",)) is not None


@pytest.mark.asyncio
async def test_visibility_endpoint_is_owner_only(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=False)
    await enable_rls(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=_api_app(db_session, test_user)), base_url="http://test"
    ) as client:
        response = await client.put(f"/api/v1/jobs/{job.id}/visibility", json={"is_public": True})
    assert response.status_code == 200, response.text
    assert response.json()["is_public"] is True

    async with AsyncClient(
        transport=ASGITransport(app=_api_app(db_session, test_user2)), base_url="http://test"
    ) as client:
        detail = await client.get(f"/api/v1/jobs/{job.id}")
        listing = await client.get("/api/v1/jobs")
        denied = await client.put(f"/api/v1/jobs/{job.id}/visibility", json={"is_public": False})

    assert detail.status_code == 200
    assert detail.json()["is_public"] is True
    assert str(job.id) not in {item["id"] for item in listing.json()["jobs"]}
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_api_artifacts_omit_uploads_for_public_viewers(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path: Path
):
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)
    job_dir = tmp_path / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "report.md").write_text("# Report")

    names: dict[str, set[str]] = {}
    for label, user in (("owner", test_user), ("viewer", test_user2)):
        with patch("openscientist.web_app.get_job_manager", return_value=_manager(tmp_path)):
            async with AsyncClient(
                transport=ASGITransport(app=_api_app(db_session, user)), base_url="http://test"
            ) as client:
                response = await client.get(f"/api/v1/jobs/{job.id}/artifacts")
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names[label] = set(zf.namelist())

    assert {"report.md", "data/cohort.csv"} <= names["owner"]
    assert "report.md" in names["viewer"]
    assert "data/cohort.csv" not in names["viewer"]


@pytest.mark.asyncio
async def test_view_sharee_of_public_job_still_gets_uploads(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path: Path
):
    job = await _job(db_session, test_user, is_public=True)
    db_session.add(
        JobShare(job_id=job.id, shared_with_user_id=test_user2.id, permission_level="view")
    )
    await db_session.commit()
    await enable_rls(db_session)
    job_dir = tmp_path / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")

    with patch("openscientist.web_app.get_job_manager", return_value=_manager(tmp_path)):
        async with AsyncClient(
            transport=ASGITransport(app=_api_app(db_session, test_user2)), base_url="http://test"
        ) as client:
            response = await client.get(f"/api/v1/jobs/{job.id}/artifacts")

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "data/cohort.csv" in zf.namelist()


@pytest.mark.asyncio
async def test_web_file_route_404s_uploads_for_public_viewers(
    committed_public_job: tuple[User, User, Job, str, str], tmp_path: Path
):
    _, _, job, owner_token, viewer_token = committed_public_job
    job_dir = tmp_path / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "report.md").write_text("# Report")

    with patch("openscientist.web_app.get_job_manager", return_value=_manager(tmp_path)):
        async with AsyncClient(
            transport=ASGITransport(app=_web_app()), base_url="http://test"
        ) as c:
            c.cookies.set("session_token", viewer_token)
            denied = await c.get(f"/web/jobs/{job.id}/files/data/cohort.csv")
            report = await c.get(f"/web/jobs/{job.id}/files/report.md")
            c.cookies.set("session_token", owner_token)
            owner_file = await c.get(f"/web/jobs/{job.id}/files/data/cohort.csv")

    assert denied.status_code == 404
    assert report.status_code == 200
    assert owner_file.status_code == 200


@pytest.mark.asyncio
async def test_web_artifacts_zip_omits_uploads_for_public_viewers(
    committed_public_job: tuple[User, User, Job, str, str], tmp_path: Path
):
    _, _, job, owner_token, viewer_token = committed_public_job
    job_dir = tmp_path / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "report.md").write_text("# Report")

    names: dict[str, set[str]] = {}
    tokens = {"owner": owner_token, "viewer": viewer_token}
    with patch("openscientist.web_app.get_job_manager", return_value=_manager(tmp_path)):
        for label in ("owner", "viewer"):
            async with AsyncClient(
                transport=ASGITransport(app=_web_app()), base_url="http://test"
            ) as client:
                client.cookies.set("session_token", tokens[label])
                response = await client.get(f"/web/jobs/{job.id}/artifacts.zip")
            assert response.status_code == 200, response.text
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                names[label] = set(zf.namelist())

    assert "data/cohort.csv" in names["owner"]
    assert "report.md" in names["viewer"]
    assert "data/cohort.csv" not in names["viewer"]


@pytest.mark.asyncio
async def test_web_report_does_not_inline_uploads_for_public_viewers(
    committed_public_job: tuple[User, User, Job, str, str], tmp_path: Path
):
    secret = b"SECRET-UPLOADED-INPUT"
    _, _, job, owner_token, viewer_token = committed_public_job
    job_dir = tmp_path / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_bytes(secret)
    (job_dir / "final_report.md").write_text("# Report\n\n![cohort](data/cohort.csv)\n")
    (job_dir / "final_report.html").write_text("<html><body>x</body></html>")

    b64 = base64.b64encode(secret).decode("ascii")
    with patch("openscientist.web_app.get_job_manager", return_value=_manager(tmp_path)):
        async with AsyncClient(
            transport=ASGITransport(app=_web_app()), base_url="http://test"
        ) as client:
            client.cookies.set("session_token", owner_token)
            owner_report = await client.get(f"/web/jobs/{job.id}/report.html")
            client.cookies.set("session_token", viewer_token)
            viewer_report = await client.get(f"/web/jobs/{job.id}/report.html")

    assert owner_report.status_code == 200, owner_report.text
    assert viewer_report.status_code == 200, viewer_report.text
    # The owner's report inlines the uploaded input; the public viewer's does not.
    assert b64 in owner_report.text
    assert b64 not in viewer_report.text


@pytest.mark.asyncio
async def test_dialog_switch_toggles_visibility_against_the_database(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=False)

    # The owner's toggle persists through the admin session the dialog uses.
    with patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)):
        is_public, owner_id = await dialog_module._load_job_public_state(str(job.id))
        assert is_public is False
        assert owner_id == str(test_user.id)

        made_public = await dialog_module._set_job_public_from_ui(
            str(job.id), True, str(test_user.id)
        )
        assert made_public[0] is True
        assert (
            await db_session.execute(select(Job.is_public).where(Job.id == job.id))
        ).scalar_one() is True

        # A non-owner is refused and the stored value is untouched.
        refused = await dialog_module._set_job_public_from_ui(
            str(job.id), False, str(test_user2.id)
        )
        assert refused == (False, "Only the job owner can change its visibility")
        assert (
            await db_session.execute(select(Job.is_public).where(Job.id == job.id))
        ).scalar_one() is True


def _switch_controller(job_id: str) -> dialog_module._ShareDialogController:
    """A share-dialog controller wired to a mocked switch and empty containers."""
    return dialog_module._ShareDialogController(
        job_id=job_id,
        shares_container=MagicMock(),
        public_switch=MagicMock(),
        email_input=MagicMock(),
        selected_user_container=MagicMock(),
        share_action_row=MagicMock(),
        permission_select=MagicMock(),
    )


@pytest.mark.asyncio
async def test_switch_loads_the_stored_flag_for_owner_and_hides_it_from_viewers(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    public_job = await _job(db_session, test_user, is_public=True)
    private_job = await _job(db_session, test_user, is_public=False)

    with patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)):
        # The owner sees the switch and it mirrors each job's stored flag.
        for job, stored in ((public_job, True), (private_job, False)):
            controller = _switch_controller(str(job.id))
            with patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)):
                await controller.refresh_public_switch()
            assert controller.public_switch.visible is True
            assert controller.public_switch.value is stored

        # A non-owner never sees the switch, public or private.
        controller = _switch_controller(str(public_job.id))
        with patch.object(dialog_module, "get_current_user_id", return_value=str(test_user2.id)):
            await controller.refresh_public_switch()
        assert controller.public_switch.visible is False


@pytest.mark.asyncio
async def test_switch_change_persists_and_refreshes(db_session: AsyncSession, test_user: User):
    from openscientist.webapp_components.utils.permissions import JobAccess

    job = await _job(db_session, test_user, is_public=False)
    controller = _switch_controller(str(job.id))
    with (
        patch.object(dialog_module, "ui") as mock_ui,
        patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)),
        patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)),
        patch.object(
            dialog_module,
            "current_job_access",
            AsyncMock(return_value=JobAccess(is_owner=True, can_edit=True)),
        ),
    ):
        await controller.on_public_change(SimpleNamespace(value=True))
    # The owner's toggle reached the database and a confirmation was shown.
    assert (
        await db_session.execute(select(Job.is_public).where(Job.id == job.id))
    ).scalar_one() is True
    mock_ui.notify.assert_called()


@pytest.mark.asyncio
async def test_switch_change_is_ignored_while_syncing(db_session: AsyncSession, test_user: User):
    job = await _job(db_session, test_user, is_public=False)
    controller = _switch_controller(str(job.id))
    controller._syncing_public_switch = True
    with (
        patch.object(dialog_module, "ui") as mock_ui,
        patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)),
        patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)),
    ):
        await controller.on_public_change(SimpleNamespace(value=True))
    # The syncing guard bails before writing or notifying.
    assert (
        await db_session.execute(select(Job.is_public).where(Job.id == job.id))
    ).scalar_one() is False
    mock_ui.notify.assert_not_called()


@pytest.mark.asyncio
async def test_switch_change_requires_sign_in(db_session: AsyncSession, test_user: User):
    job = await _job(db_session, test_user, is_public=False)
    controller = _switch_controller(str(job.id))
    with (
        patch.object(dialog_module, "ui") as mock_ui,
        patch.object(dialog_module, "get_current_user_id", return_value=None),
        patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)),
    ):
        await controller.on_public_change(SimpleNamespace(value=True))
    # No signed-in user means no write, and the switch is not refreshed.
    assert (
        await db_session.execute(select(Job.is_public).where(Job.id == job.id))
    ).scalar_one() is False
    mock_ui.notify.assert_called()


@pytest.mark.asyncio
async def test_switch_load_failure_leaves_the_switch_untouched(
    db_session: AsyncSession, test_user: User
):
    job = await _job(db_session, test_user, is_public=True)
    controller = _switch_controller(str(job.id))

    @contextlib.asynccontextmanager
    async def _broken():
        raise RuntimeError("database unavailable")
        yield  # never reached

    with (
        patch.object(dialog_module, "get_admin_session", _broken),
        patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)),
    ):
        await controller.refresh_public_switch()  # must not raise
    # The failed load never assigned the switch a real visibility value.
    assert not isinstance(controller.public_switch.visible, bool)


@pytest.mark.asyncio
async def test_switch_change_failure_notifies_an_error(db_session: AsyncSession, test_user: User):
    job = await _job(db_session, test_user, is_public=False)
    controller = _switch_controller(str(job.id))
    with (
        patch.object(dialog_module, "ui") as mock_ui,
        patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)),
        patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)),
        patch.object(
            dialog_module,
            "_set_job_public_from_ui",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        await controller.on_public_change(SimpleNamespace(value=True))
    # The failed write left the flag untouched and surfaced an error.
    assert (
        await db_session.execute(select(Job.is_public).where(Job.id == job.id))
    ).scalar_one() is False
    mock_ui.notify.assert_called()


@pytest.mark.asyncio
async def test_refresh_shares_reloads_visibility(db_session: AsyncSession, test_user: User):
    job = await _job(db_session, test_user, is_public=True)
    controller = _switch_controller(str(job.id))
    with (
        patch.object(dialog_module, "ui"),
        patch.object(dialog_module, "get_current_user_id", return_value=str(test_user.id)),
        patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)),
    ):
        await controller.refresh_shares()
    # refresh_shares reloaded the switch from the database.
    assert controller.public_switch.visible is True
    assert controller.public_switch.value is True


@pytest.mark.asyncio
async def test_load_public_state_reports_a_missing_job(db_session: AsyncSession):
    missing = str(UUID(int=0))  # a job id that exists in no row
    with patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)):
        is_public, owner_id = await dialog_module._load_job_public_state(missing)
    assert is_public is False
    assert owner_id is None


@pytest.mark.asyncio
async def test_set_public_to_private_returns_the_private_message(
    db_session: AsyncSession, test_user: User
):
    job = await _job(db_session, test_user, is_public=True)
    with patch.object(dialog_module, "get_admin_session", fake_admin_session(db_session)):
        success, _message = await dialog_module._set_job_public_from_ui(
            str(job.id), False, str(test_user.id)
        )
    assert success is True
    assert (
        await db_session.execute(select(Job.is_public).where(Job.id == job.id))
    ).scalar_one() is False


@pytest.mark.asyncio
async def test_owner_name_lookup_reads_the_owner_name(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=True)
    with patch("openscientist.database.session.get_admin_session", fake_admin_session(db_session)):
        name = await _owner_name(str(job.id), is_owner=False)
    # A viewer is told the owner's display name, never their email.
    assert name == test_user.name
    assert name != test_user.email
    with patch("openscientist.database.session.get_admin_session", fake_admin_session(db_session)):
        assert await _owner_name(str(job.id), is_owner=True) is None


def test_stats_badges_mark_a_public_job() -> None:
    public = JobInfo(
        job_id="job-1",
        research_question="Q",
        status=JobStatus.COMPLETED,
        created_at="2026-01-01T00:00:00",
        is_public=True,
    )
    private = JobInfo(
        job_id="job-2",
        research_question="Q",
        status=JobStatus.COMPLETED,
        created_at="2026-01-01T00:00:00",
        is_public=False,
    )
    assert ("Public", "anyone signed in", "teal") in timeline._stats_badges(public, 0)
    assert not any(label == "Public" for label, _, _ in timeline._stats_badges(private, 0))


def test_stats_content_shows_the_owner_label_to_viewers() -> None:
    job = JobInfo(
        job_id="job-1",
        research_question="Q",
        status=JobStatus.COMPLETED,
        created_at="2026-01-01T00:00:00",
        is_public=True,
    )
    owner = "Dr. Ada Lovelace"
    context = SimpleNamespace(job_info=job, ks_data=None, owner_name=owner)
    with (
        patch.object(timeline, "ui") as mock_ui,
        patch.object(timeline, "is_client_connected", return_value=True),
        patch.object(timeline, "render_stat_badges"),
    ):
        timeline._render_job_stats_content(context)  # type: ignore[arg-type]
    # The viewer is told who owns the job.
    labels = [c.args[0] for c in mock_ui.label.call_args_list if c.args]
    assert any(owner in label for label in labels)
