"""
Tests for public job visibility (issue #296).

A job owner can make a job readable by any signed-in user. Row-level security
admits the job and its analysis rows, but not its uploaded inputs, chat or
costs, and only the owner may change the flag.
"""

import io
import zipfile
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.artifact_packager import create_artifacts_zip_file
from openscientist.database.models import (
    CostRecord,
    Finding,
    Job,
    JobChatMessage,
    JobDataFile,
    JobShare,
    User,
)
from openscientist.database.rls import set_current_user
from openscientist.share_service import set_job_public, viewer_has_direct_access
from tests.helpers import enable_rls


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


async def _visible_job(db_session: AsyncSession, job: Job) -> Job | None:
    result = await db_session.execute(select(Job).where(Job.id == job.id))
    return result.scalar_one_or_none()


@pytest.mark.asyncio
async def test_new_jobs_are_private_by_default(db_session: AsyncSession, test_user: User):
    job = Job(owner_id=test_user.id, research_question="Default", status="pending")
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    assert job.is_public is False


@pytest.mark.asyncio
async def test_public_job_is_readable_by_any_signed_in_user(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    public_job = await _job(db_session, test_user, is_public=True)
    private_job = await _job(db_session, test_user, is_public=False)
    await enable_rls(db_session)

    await set_current_user(db_session, test_user2.id)

    assert await _visible_job(db_session, public_job) is not None
    assert await _visible_job(db_session, private_job) is None


@pytest.mark.asyncio
async def test_public_job_is_not_readable_without_a_signed_in_user(
    db_session: AsyncSession, test_user: User
):
    public_job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)

    await set_current_user(db_session, None)
    assert await _visible_job(db_session, public_job) is None

    await db_session.execute(text("SELECT set_config('app.current_user_id', '', false)"))
    assert await _visible_job(db_session, public_job) is None


@pytest.mark.asyncio
async def test_public_job_exposes_analysis_rows_but_not_inputs_chat_or_costs(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=True)
    db_session.add_all(
        [
            Finding(
                job_id=job.id,
                iteration=1,
                text="Astrocyte markers are reduced",
                finding_type="observation",
                source="analysis",
            ),
            JobDataFile(
                job_id=job.id,
                filename="cohort.csv",
                file_path="data/cohort.csv",
                file_type="csv",
                file_size=10,
            ),
            JobChatMessage(job_id=job.id, role="user", content="private note"),
            CostRecord(
                job_id=job.id,
                operation_type="agent",
                provider="anthropic",
                model="test-model",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.01,
            ),
        ]
    )
    await db_session.commit()
    await enable_rls(db_session)

    await set_current_user(db_session, test_user2.id)

    findings = (await db_session.execute(select(Finding).where(Finding.job_id == job.id))).all()
    assert len(findings) == 1
    for model in (JobDataFile, JobChatMessage, CostRecord):
        rows = (await db_session.execute(select(model).where(model.job_id == job.id))).all()
        assert rows == [], f"{model.__name__} rows leaked from a public job"

    await set_current_user(db_session, test_user.id)
    for model in (JobDataFile, JobChatMessage, CostRecord):
        rows = (await db_session.execute(select(model).where(model.job_id == job.id))).all()
        assert len(rows) == 1, f"owner lost access to {model.__name__}"


@pytest.mark.asyncio
async def test_public_job_is_read_only_for_other_users(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)
    await set_current_user(db_session, test_user2.id)

    await db_session.execute(
        update(Job).where(Job.id == job.id).values(research_question="changed")
    )

    await set_current_user(db_session, test_user.id)
    stored = await db_session.execute(select(Job.research_question).where(Job.id == job.id))
    assert stored.scalar_one() == "Public visibility job"


@pytest.mark.asyncio
async def test_owner_can_toggle_is_public(db_session: AsyncSession, test_user: User):
    job = await _job(db_session, test_user, is_public=False)
    await enable_rls(db_session)
    await set_current_user(db_session, test_user.id)

    await db_session.execute(update(Job).where(Job.id == job.id).values(is_public=True))
    stored = await db_session.execute(select(Job.is_public).where(Job.id == job.id))

    assert stored.scalar_one() is True


@pytest.mark.asyncio
async def test_edit_share_cannot_make_a_job_public(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    """RLS lets an edit share UPDATE jobs; the trigger keeps is_public owner-only."""
    job = await _job(db_session, test_user, is_public=False)
    db_session.add(
        JobShare(job_id=job.id, shared_with_user_id=test_user2.id, permission_level="edit")
    )
    await db_session.commit()
    await enable_rls(db_session)
    await set_current_user(db_session, test_user2.id)

    # A savepoint keeps the refused statement from aborting the fixture's transaction.
    with pytest.raises(DBAPIError, match="only the job owner can change is_public"):
        async with db_session.begin_nested():
            await db_session.execute(update(Job).where(Job.id == job.id).values(is_public=True))


def test_artifacts_zip_can_leave_out_uploaded_inputs(tmp_path):
    job_dir = tmp_path / "job"
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "provenance").mkdir()
    (job_dir / "provenance" / "data").mkdir()
    (job_dir / "provenance" / "data" / "derived.csv").write_text("x\n")
    (job_dir / "report.md").write_text("# Report")
    archive = tmp_path / "out.zip"

    create_artifacts_zip_file(job_dir, archive, "job", excluded_top_level_dirs=("data",))

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "report.md" in names
    assert "provenance/data/derived.csv" in names
    assert not any(name.startswith("data/") for name in names)


def _api_app(db_session: AsyncSession, user: User):
    from fastapi import FastAPI

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
async def test_public_viewer_artifacts_omit_uploads_but_owner_gets_them(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path
):
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)
    job_dir = tmp_path / "jobs" / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")
    (job_dir / "report.md").write_text("# Report")

    names: dict[str, set[str]] = {}
    for label, user in (("owner", test_user), ("viewer", test_user2)):
        with patch(
            "openscientist.api.endpoints.jobs._get_jobs_dir", return_value=tmp_path / "jobs"
        ):
            async with AsyncClient(
                transport=ASGITransport(app=_api_app(db_session, user)),
                base_url="http://test",
            ) as client:
                response = await client.get(f"/api/v1/jobs/{job.id}/artifacts")
        assert response.status_code == 200, response.text
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names[label] = set(zf.namelist())

    assert {"report.md", "data/cohort.csv"} <= names["owner"]
    assert "report.md" in names["viewer"]
    assert "data/cohort.csv" not in names["viewer"]


@pytest.mark.asyncio
async def test_view_share_recipient_of_a_public_job_still_gets_uploads(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path
):
    job = await _job(db_session, test_user, is_public=True)
    db_session.add(
        JobShare(job_id=job.id, shared_with_user_id=test_user2.id, permission_level="view")
    )
    await db_session.commit()
    await enable_rls(db_session)
    job_dir = tmp_path / "jobs" / str(job.id)
    (job_dir / "data").mkdir(parents=True)
    (job_dir / "data" / "cohort.csv").write_text("id\n1\n")

    with patch("openscientist.api.endpoints.jobs._get_jobs_dir", return_value=tmp_path / "jobs"):
        async with AsyncClient(
            transport=ASGITransport(app=_api_app(db_session, test_user2)),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/api/v1/jobs/{job.id}/artifacts")

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "data/cohort.csv" in zf.namelist()


@pytest.mark.asyncio
async def test_set_job_public_is_owner_only(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=False)
    await enable_rls(db_session)

    await set_current_user(db_session, test_user.id)
    updated = await set_job_public(
        db_session, test_user.id, job_id=str(job.id), is_public=True, not_owned_detail="no"
    )
    assert updated.is_public is True

    await set_current_user(db_session, test_user2.id)
    with pytest.raises(HTTPException) as refused:
        await set_job_public(
            db_session, test_user2.id, job_id=str(job.id), is_public=False, not_owned_detail="no"
        )
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_viewer_has_direct_access_only_for_owner_and_share_recipients(
    db_session: AsyncSession, test_user: User, test_user2: User
):
    job = await _job(db_session, test_user, is_public=True)
    await enable_rls(db_session)

    await set_current_user(db_session, test_user.id)
    assert await viewer_has_direct_access(db_session, job, test_user.id) is True
    await set_current_user(db_session, test_user2.id)
    assert await viewer_has_direct_access(db_session, job, test_user2.id) is False
