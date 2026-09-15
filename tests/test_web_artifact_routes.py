"""Tests for the session-authenticated artifact download route."""

import io
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth.middleware import get_current_user_id
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.webapp_components.artifact_routes import router
from tests.helpers import enable_rls


def _build_app(db_session: AsyncSession, acting_user_id: UUID) -> FastAPI:
    app = FastAPI()

    async def override_get_session():
        await set_current_user(db_session, acting_user_id)
        yield db_session

    async def override_get_current_user_id():
        return acting_user_id

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_id] = override_get_current_user_id
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_web_download_streams_zip_for_owner(
    db_session: AsyncSession,
    test_user: User,
    tmp_path: Path,
) -> None:
    job = Job(
        owner_id=test_user.id,
        research_question="Artifacts download job",
        description="artifact route test",
        status="completed",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    await enable_rls(db_session)

    job_dir = tmp_path / str(job.id)
    job_dir.mkdir(parents=True)
    (job_dir / "report.md").write_text("# Report")

    app = _build_app(db_session, test_user.id)

    class _Manager:
        jobs_dir = tmp_path

    with patch("openscientist.web_app.get_job_manager", return_value=_Manager()):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/web/jobs/{job.id}/artifacts.zip")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert f"{job.id}_artifacts.zip" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "report.md" in zf.namelist()

    leftovers = list(Path(tempfile.gettempdir()).glob(f"openscientist_{job.id}_*"))
    assert leftovers == []


@pytest.mark.asyncio
async def test_web_download_forbidden_for_non_owner(
    db_session: AsyncSession,
    test_user: User,
    test_user2: User,
    tmp_path: Path,
) -> None:
    job = Job(
        owner_id=test_user.id,
        research_question="Private job",
        description="artifact route test",
        status="completed",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    await enable_rls(db_session)

    app = _build_app(db_session, test_user2.id)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/web/jobs/{job.id}/artifacts.zip")

    assert response.status_code == 404
