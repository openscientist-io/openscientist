"""Public job visibility: the API toggle, file routes and the dialog switch.

The owner flips a job public; any signed-in user then reads the job and its
analysis rows but not its uploaded inputs (``data/``), chat or costs. The
owner and share recipients keep everything. Only the owner may change the flag.
"""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.artifact_packager import artifact_file, create_artifacts_zip_file
from openscientist.database.models import Job, JobShare, User
from openscientist.database.rls import set_current_user
from openscientist.share_service import (
    PUBLIC_VIEWER_EXCLUDED_DIRS,
    set_job_public,
    viewer_has_direct_access,
    withheld_artifact_dirs,
)
from openscientist.webapp_components.components import dialogs as dialog_module
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


def _web_app(db_session: AsyncSession, user: User) -> FastAPI:
    from openscientist.auth.middleware import get_current_user_id
    from openscientist.database.session import get_session
    from openscientist.webapp_components.artifact_routes import router

    app = FastAPI()

    async def override_get_session():
        await set_current_user(db_session, user.id)
        yield db_session

    async def override_get_current_user_id():
        return str(user.id)

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_id] = override_get_current_user_id
    app.include_router(router)
    return app


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
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path: Path
):
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)
    job_dir = tmp_path / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "report.md").write_text("# Report")

    with patch("openscientist.web_app.get_job_manager", return_value=_manager(tmp_path)):
        async with AsyncClient(
            transport=ASGITransport(app=_web_app(db_session, test_user2)), base_url="http://test"
        ) as client:
            denied = await client.get(f"/web/jobs/{job.id}/files/data/cohort.csv")
            report = await client.get(f"/web/jobs/{job.id}/files/report.md")
        async with AsyncClient(
            transport=ASGITransport(app=_web_app(db_session, test_user)), base_url="http://test"
        ) as client:
            owner_file = await client.get(f"/web/jobs/{job.id}/files/data/cohort.csv")

    assert denied.status_code == 404
    assert report.status_code == 200
    assert owner_file.status_code == 200


@pytest.mark.asyncio
async def test_web_artifacts_zip_omits_uploads_for_public_viewers(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path: Path
):
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)
    job_dir = tmp_path / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "report.md").write_text("# Report")

    names: dict[str, set[str]] = {}
    with patch("openscientist.web_app.get_job_manager", return_value=_manager(tmp_path)):
        for label, user in (("owner", test_user), ("viewer", test_user2)):
            async with AsyncClient(
                transport=ASGITransport(app=_web_app(db_session, user)), base_url="http://test"
            ) as client:
                response = await client.get(f"/web/jobs/{job.id}/artifacts.zip")
            assert response.status_code == 200, response.text
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                names[label] = set(zf.namelist())

    assert "data/cohort.csv" in names["owner"]
    assert "report.md" in names["viewer"]
    assert "data/cohort.csv" not in names["viewer"]


@pytest.mark.asyncio
async def test_web_report_does_not_inline_uploads_for_public_viewers(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path: Path
):
    secret = b"SECRET-UPLOADED-INPUT"
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)
    job_dir = tmp_path / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_bytes(secret)
    (job_dir / "final_report.md").write_text("# Report\n\n![cohort](data/cohort.csv)\n")
    (job_dir / "final_report.html").write_text("<html><body>x</body></html>")

    b64 = base64.b64encode(secret).decode("ascii")
    with patch("openscientist.web_app.get_job_manager", return_value=_manager(tmp_path)):
        async with AsyncClient(
            transport=ASGITransport(app=_web_app(db_session, test_user)), base_url="http://test"
        ) as client:
            owner_report = await client.get(f"/web/jobs/{job.id}/report.html")
        async with AsyncClient(
            transport=ASGITransport(app=_web_app(db_session, test_user2)), base_url="http://test"
        ) as client:
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
