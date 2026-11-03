"""Tests for the session-authenticated artifact download route."""

import io
import json
import tempfile
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Job, Session, User
from openscientist.webapp_components.artifact_routes import router


def _web_app() -> FastAPI:
    """The artifact router on the app's real per-request sessions."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def owner_with_job(
    test_engine: AsyncEngine,
) -> AsyncIterator[tuple[User, str, Job]]:
    """A committed owner, the id of their live session and a completed job
    of theirs, so the route's real per-request sessions read the rows the
    web's do."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(
            email=f"artifact-owner-{uuid4().hex}@artifact.test",
            name="Artifact Owner",
        )
        db.add(owner)
        await db.flush()
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        job = Job(
            owner_id=owner.id,
            research_question="Artifacts download job",
            description="artifact route test",
            status="completed",
            version_info={"openscientist_commit": "abc123def456"},
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
    yield owner, str(login.id), job
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest_asyncio.fixture
async def signed_in_viewer(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, str]]:
    """A committed non-owner and the id of their live session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        viewer = User(
            email=f"artifact-viewer-{uuid4().hex}@artifact.test",
            name="Artifact Viewer",
        )
        db.add(viewer)
        await db.flush()
        login = Session(user_id=viewer.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield viewer, str(login.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == viewer.id))
        await db.commit()


class _Manager:
    jobs_dir: Path


@pytest.mark.asyncio
async def test_web_download_streams_zip_for_owner(
    owner_with_job: tuple[User, str, Job],
    tmp_path: Path,
):
    _, token, job = owner_with_job

    job_dir = tmp_path / str(job.id)
    job_dir.mkdir(parents=True)
    (job_dir / "report.md").write_text("# Report")

    manager = _Manager()
    manager.jobs_dir = tmp_path

    with patch("openscientist.web_app.get_job_manager", return_value=manager):
        async with AsyncClient(
            transport=ASGITransport(app=_web_app()),
            base_url="http://test",
        ) as client:
            client.cookies.set("session_token", token)
            response = await client.get(f"/web/jobs/{job.id}/artifacts.zip")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert f"{job.id}_artifacts.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "report.md" in zf.namelist()
        assert json.loads(zf.read("provenance.json")) == {"openscientist_commit": "abc123def456"}

    leftovers = list(Path(tempfile.gettempdir()).glob(f"openscientist_{job.id}_*"))
    assert leftovers == []


@pytest.mark.asyncio
async def test_web_download_forbidden_for_non_owner(
    owner_with_job: tuple[User, str, Job],
    signed_in_viewer: tuple[User, str],
):
    _, _, job = owner_with_job
    _, viewer_token = signed_in_viewer

    async with AsyncClient(
        transport=ASGITransport(app=_web_app()),
        base_url="http://test",
    ) as client:
        client.cookies.set("session_token", viewer_token)
        response = await client.get(f"/web/jobs/{job.id}/artifacts.zip")

    assert response.status_code == 404
