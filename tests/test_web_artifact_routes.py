"""Tests for the session-authenticated artifact download route."""

import io
import re
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.auth.middleware import get_current_user_id
from openscientist.database.models import Job, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.webapp_components.artifact_routes import router
from openscientist.webapp_components.pages import job_detail
from tests.helpers import enable_rls


def _build_app(db_session: AsyncSession, acting_user_id) -> FastAPI:
    app = FastAPI()

    async def override_get_session():
        # Mirrors get_session, which sets the app role and leaves the user to the route.
        await set_current_user(db_session, None)
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
    tmp_path,
):
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
    tmp_path,
):
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


async def _private_job(db_session: AsyncSession, owner: User, jobs_dir: Path) -> tuple[Job, Path]:
    job = Job(
        owner_id=owner.id,
        research_question="Job file route job",
        description="job file route test",
        status="completed",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    await enable_rls(db_session)
    job_dir = jobs_dir / str(job.id)
    (job_dir / "provenance").mkdir(parents=True)
    (job_dir / "data").mkdir()
    return job, job_dir


async def _get(db_session: AsyncSession, user: User, jobs_dir: Path, path: str):
    manager = SimpleNamespace(jobs_dir=jobs_dir)
    with patch("openscientist.web_app.get_job_manager", return_value=manager):
        async with AsyncClient(
            transport=ASGITransport(app=_build_app(db_session, user.id)),
            base_url="http://test",
        ) as client:
            return await client.get(path)


@pytest.mark.asyncio
async def test_plot_is_served_to_the_owner(db_session: AsyncSession, test_user: User, tmp_path):
    job, job_dir = await _private_job(db_session, test_user, tmp_path)
    (job_dir / "provenance" / "volcano.png").write_bytes(b"\x89PNG plot")

    response = await _get(db_session, test_user, tmp_path, f"/web/jobs/{job.id}/plots/volcano.png")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"\x89PNG plot"


@pytest.mark.asyncio
async def test_plot_is_not_found_for_a_user_without_access(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path
):
    job, job_dir = await _private_job(db_session, test_user, tmp_path)
    (job_dir / "provenance" / "volcano.png").write_bytes(b"\x89PNG plot")

    response = await _get(db_session, test_user2, tmp_path, f"/web/jobs/{job.id}/plots/volcano.png")

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "notes.csv",  # not a plot
        "linked.png",  # symlink out of provenance/
        "..%2Fdata%2Fcohort.png",  # encoded traversal
        "missing.png",
    ],
)
async def test_plot_route_serves_only_png_files_directly_in_provenance(
    db_session: AsyncSession, test_user: User, tmp_path, name: str
):
    job, job_dir = await _private_job(db_session, test_user, tmp_path)
    (job_dir / "provenance" / "notes.csv").write_text("a,b\n")
    (job_dir / "data" / "cohort.png").write_bytes(b"uploaded input")
    (job_dir / "provenance" / "linked.png").symlink_to(job_dir / "data" / "cohort.png")

    response = await _get(db_session, test_user, tmp_path, f"/web/jobs/{job.id}/plots/{name}")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_report_html_is_rendered_for_the_owner(
    db_session: AsyncSession, test_user: User, tmp_path
):
    job, job_dir = await _private_job(db_session, test_user, tmp_path)
    (job_dir / "final_report.md").write_text("# Findings\n\nAstrocyte markers drop.\n")
    (job_dir / "final_report.html").write_text("<html><body>stale</body></html>")

    response = await _get(db_session, test_user, tmp_path, f"/web/jobs/{job.id}/report.html")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "Astrocyte markers drop." in response.text


@pytest.mark.asyncio
async def test_report_html_is_not_found_for_a_user_without_access(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path
):
    job, job_dir = await _private_job(db_session, test_user, tmp_path)
    (job_dir / "final_report.md").write_text("# Findings\n\nAstrocyte markers drop.\n")
    (job_dir / "final_report.html").write_text("<html><body>secret</body></html>")

    response = await _get(db_session, test_user2, tmp_path, f"/web/jobs/{job.id}/report.html")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_report_falls_back_to_the_stored_html_when_it_cannot_be_rendered(
    db_session: AsyncSession, test_user: User, tmp_path
):
    job, job_dir = await _private_job(db_session, test_user, tmp_path)
    (job_dir / "final_report.html").write_text("<html><body>stored report</body></html>")

    response = await _get(db_session, test_user, tmp_path, f"/web/jobs/{job.id}/report.html")

    assert response.status_code == 200, response.text
    assert "stored report" in response.text


@pytest.mark.asyncio
async def test_report_is_not_found_before_it_is_generated(
    db_session: AsyncSession, test_user: User, tmp_path
):
    job, _job_dir = await _private_job(db_session, test_user, tmp_path)

    response = await _get(db_session, test_user, tmp_path, f"/web/jobs/{job.id}/report.html")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_job_without_a_directory_is_not_found(
    db_session: AsyncSession, test_user: User, tmp_path
):
    job, _job_dir = await _private_job(db_session, test_user, tmp_path / "elsewhere")

    response = await _get(db_session, test_user, tmp_path, f"/web/jobs/{job.id}/report.html")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_job_page_plot_image_loads_from_the_plot_route(
    db_session: AsyncSession, test_user: User, tmp_path
):
    job, job_dir = await _private_job(db_session, test_user, tmp_path)
    (job_dir / "provenance" / "volcano.png").write_bytes(b"\x89PNG plot")
    (job_dir / "provenance" / "volcano.json").write_text('{"iteration": 1}')

    with patch.object(job_detail, "ui") as mock_ui:
        job_detail._render_iteration_plots(job_dir / "provenance", 1)
    src = mock_ui.image.call_args.args[0]

    response = await _get(db_session, test_user, tmp_path, src)
    assert response.status_code == 200, src
    assert response.content == b"\x89PNG plot"


@pytest.mark.asyncio
async def test_job_page_report_frame_loads_from_the_report_route(
    db_session: AsyncSession, test_user: User, tmp_path
):
    job, job_dir = await _private_job(db_session, test_user, tmp_path)
    (job_dir / "final_report.md").write_text("# Findings\n\nAstrocyte markers drop.\n")
    (job_dir / "final_report.html").write_text("<html><body>stored report</body></html>")

    with patch.object(job_detail, "ui") as mock_ui:
        job_detail._render_report_html_iframe(str(job.id))
    props = mock_ui.element.return_value.props.call_args.args[0]
    src = re.search(r'src="([^"]+)"', props).group(1)  # type: ignore[union-attr]

    response = await _get(db_session, test_user, tmp_path, src)
    assert response.status_code == 200, src
    assert "Astrocyte markers drop." in response.text
