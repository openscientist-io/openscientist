"""A job's files and report reach only users who may see the job
(plans/job-file-exposure.md)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.admin.orphan_jobs import assign_orphaned_job
from openscientist.auth.middleware import get_current_user_id
from openscientist.database.models import Administrator, Job, JobShare, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session
from openscientist.webapp_components.artifact_routes import router
from tests.helpers import enable_rls

_PLOT = b"\x89PNG\r\n\x1a\nplot-bytes"


@dataclass(frozen=True)
class _SharedJob:
    job_id: UUID
    job_dir: Path
    owner: UUID
    viewer: UUID
    stranger: UUID


@pytest_asyncio.fixture
async def shared_job(
    db_session: AsyncSession, test_user: User, test_user2: User, tmp_path: Path
) -> _SharedJob:
    """A completed job of ``test_user``, shared for viewing with ``test_user2``,
    whose directory holds artifacts next to the agent's private files."""
    stranger = User(email=f"stranger-{uuid4().hex}@files.test", name="Stranger")
    job = Job(owner_id=test_user.id, research_question="Whose files?", status="completed")
    db_session.add_all([stranger, job])
    await db_session.flush()
    db_session.add(
        JobShare(job_id=job.id, shared_with_user_id=test_user2.id, permission_level="view")
    )
    await db_session.commit()
    await enable_rls(db_session)

    job_dir = tmp_path / str(job.id)
    (job_dir / "provenance").mkdir(parents=True)
    (job_dir / "provenance" / "plot.png").write_bytes(_PLOT)
    (job_dir / "final_report.md").write_text("# Current report\n\nWater boils.\n")
    (job_dir / "final_report.html").write_text("<html><body>Current report</body></html>")
    for private in (".omp-home/agent.db", ".codex/auth.json", ".omp/mcp.json", "config.json"):
        (job_dir / private).parent.mkdir(parents=True, exist_ok=True)
        (job_dir / private).write_text("operator-secret")
    other = tmp_path / "other-job"
    other.mkdir()
    (other / "secret.txt").write_text("operator-secret")
    (job_dir / "linked.txt").symlink_to(other / "secret.txt")
    (job_dir / "linked-dir").symlink_to(other, target_is_directory=True)
    return _SharedJob(job.id, job_dir, test_user.id, test_user2.id, stranger.id)


@pytest_asyncio.fixture
async def request_as(db_session: AsyncSession, shared_job: _SharedJob) -> AsyncIterator[object]:
    """``request_as(user_id, path)``: GET ``path`` with ``user_id``'s web
    session, or with none when ``user_id`` is None."""

    class _Manager:
        jobs_dir = shared_job.job_dir.parent

    async def get(user_id: UUID | None, path: str):  # type: ignore[no-untyped-def]
        app = FastAPI()

        async def session_as_user() -> AsyncIterator[AsyncSession]:
            if user_id is not None:
                await set_current_user(db_session, user_id)
            yield db_session

        app.dependency_overrides[get_session] = session_as_user
        app.dependency_overrides[get_current_user_id] = lambda: user_id
        app.include_router(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(path)

    with patch("openscientist.web_app.get_job_manager", return_value=_Manager()):
        yield get


@pytest.mark.asyncio
@pytest.mark.parametrize(("who", "status"), [("owner", 200), ("viewer", 200), ("stranger", 404)])
async def test_a_plot_reaches_the_owner_and_a_share_viewer_only(
    shared_job: _SharedJob, request_as, who: str, status: int
) -> None:
    response = await request_as(
        getattr(shared_job, who), f"/web/jobs/{shared_job.job_id}/files/provenance/plot.png"
    )
    assert response.status_code == status
    if status == 200:
        assert response.content == _PLOT


@pytest.mark.asyncio
async def test_a_visitor_without_a_session_gets_no_file(shared_job: _SharedJob, request_as) -> None:
    response = await request_as(None, f"/web/jobs/{shared_job.job_id}/files/provenance/plot.png")
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        ".omp-home/agent.db",
        ".codex/auth.json",
        ".omp/mcp.json",
        "config.json",
        "%2E%2E/other-job/secret.txt",
        "linked.txt",
        "linked-dir/secret.txt",
        "provenance",
        "missing.png",
    ],
)
async def test_agent_files_and_unsafe_paths_are_refused_even_to_the_owner(
    shared_job: _SharedJob, request_as, path: str
) -> None:
    response = await request_as(shared_job.owner, f"/web/jobs/{shared_job.job_id}/files/{path}")
    assert response.status_code == 404
    assert b"operator-secret" not in response.content


@pytest.mark.asyncio
async def test_the_report_shows_its_current_version_to_the_owner_only(
    shared_job: _SharedJob, request_as
) -> None:
    url = f"/web/jobs/{shared_job.job_id}/report.html"
    first = await request_as(shared_job.owner, url)
    assert first.status_code == 200
    assert "Water boils." in first.text
    (shared_job.job_dir / "final_report.md").write_text("# Regenerated\n\nWater still boils.\n")
    again = await request_as(shared_job.owner, url)
    assert "Water still boils." in again.text
    assert (await request_as(shared_job.stranger, url)).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["files/provenance/plot.png", "report.html"])
async def test_what_the_agent_wrote_is_served_sandboxed(
    shared_job: _SharedJob, request_as, path: str
) -> None:
    response = await request_as(shared_job.owner, f"/web/jobs/{shared_job.job_id}/{path}")
    assert response.status_code == 200
    policy = [part.strip() for part in response.headers["content-security-policy"].split(";")]
    sandbox = next(part for part in policy if part.startswith("sandbox"))
    assert "allow-same-origin" not in sandbox
    assert response.headers["x-content-type-options"] == "nosniff"


def _csp_directives(policy: str) -> dict[str, str]:
    directives: dict[str, str] = {}
    for part in policy.split(";"):
        part = part.strip()
        if part:
            name, _, sources = part.partition(" ")
            directives[name] = sources.strip()
    return directives


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["files/provenance/plot.png", "report.html"])
async def test_agent_content_cannot_reach_other_hosts(
    shared_job: _SharedJob, request_as, path: str
) -> None:
    response = await request_as(shared_job.owner, f"/web/jobs/{shared_job.job_id}/{path}")
    assert response.status_code == 200
    directives = _csp_directives(response.headers["content-security-policy"])

    assert directives["connect-src"] == "'none'"
    assert set(directives["img-src"].split()) == {"'self'", "data:"}


@pytest.mark.asyncio
async def test_the_report_allows_framing_only_from_the_app(
    shared_job: _SharedJob, request_as
) -> None:
    response = await request_as(shared_job.owner, f"/web/jobs/{shared_job.job_id}/report.html")
    assert response.status_code == 200
    directives = _csp_directives(response.headers["content-security-policy"])

    assert directives["frame-ancestors"] == "'self'"


@pytest_asyncio.fixture
async def committed_job(test_engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[_SharedJob]:
    """A committed job with a plot, which the web's own login reads through RLS."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        owner = User(email=f"owner-{uuid4().hex}@files.test", name="Owner")
        stranger = User(email=f"stranger-{uuid4().hex}@files.test", name="Stranger")
        session.add_all([owner, stranger])
        await session.flush()
        job = Job(owner_id=owner.id, research_question="Mine?", status="completed")
        session.add(job)
        await session.commit()
    job_dir = tmp_path / str(job.id)
    (job_dir / "provenance").mkdir(parents=True)
    (job_dir / "provenance" / "plot.png").write_bytes(_PLOT)
    yield _SharedJob(job.id, job_dir, owner.id, stranger.id, stranger.id)
    async with AsyncSession(test_engine) as session:
        await session.execute(delete(Job).where(Job.id == job.id))
        await session.execute(delete(User).where(User.id.in_([owner.id, stranger.id])))
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(("who", "status"), [("owner", 200), ("stranger", 404)])
async def test_a_signed_in_user_is_resolved_through_the_webs_own_login(
    web_login: None, committed_job: _SharedJob, who: str, status: int
) -> None:
    """The session's user id comes from the browser's storage, as a string, and
    the web login reads users only under RLS, so resolving the user must not
    depend on a test session that bypasses it."""

    class _Manager:
        jobs_dir = committed_job.job_dir.parent

    app = FastAPI()
    user_id = str(getattr(committed_job, who))
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    app.include_router(router)
    url = f"/web/jobs/{committed_job.job_id}/files/provenance/plot.png"
    with patch("openscientist.web_app.get_job_manager", return_value=_Manager()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get(url)
    assert response.status_code == status


@pytest.mark.asyncio
async def test_a_malformed_session_user_is_signed_out(shared_job: _SharedJob, db_session) -> None:
    class _Manager:
        jobs_dir = shared_job.job_dir.parent

    async def session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app = FastAPI()
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_current_user_id] = lambda: "not-a-user-id"
    app.include_router(router)
    url = f"/web/jobs/{shared_job.job_id}/files/provenance/plot.png"
    with patch("openscientist.web_app.get_job_manager", return_value=_Manager()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get(url)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_an_artifact_over_the_size_limit_is_not_served(
    shared_job: _SharedJob, request_as
) -> None:
    url = f"/web/jobs/{shared_job.job_id}/files/provenance/plot.png"
    with patch("openscientist.artifact_packager.MAX_ARTIFACT_FILE_SIZE_BYTES", len(_PLOT) - 1):
        response = await request_as(shared_job.owner, url)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_report_falls_back_to_its_saved_html(shared_job: _SharedJob, request_as) -> None:
    """A report whose figures cannot be re-rendered still shows, as last saved."""
    with patch(
        "openscientist.report.renderer.render_report_html", side_effect=ValueError("bad figure")
    ):
        response = await request_as(shared_job.owner, f"/web/jobs/{shared_job.job_id}/report.html")
    assert response.status_code == 200
    assert "Current report" in response.text
    assert "window.open" in response.text


@pytest.mark.asyncio
async def test_the_plot_the_page_shows_reaches_its_owner(
    shared_job: _SharedJob, request_as
) -> None:
    from openscientist.webapp_components.pages.job_detail import iterations

    provenance = shared_job.job_dir / "provenance"
    (provenance / "dose response #1.png").write_bytes(_PLOT)
    (provenance / "dose response #1.json").write_text('{"iteration": 1}')
    with patch.object(iterations, "ui") as ui:
        iterations._render_iteration_plots(provenance, 1)
    [url] = [c.args[0] for c in ui.image.call_args_list]

    response = await request_as(shared_job.owner, url)
    assert response.status_code == 200
    assert response.content == _PLOT


@pytest.mark.asyncio
async def test_the_report_the_page_frames_reaches_its_owner(
    shared_job: _SharedJob, request_as
) -> None:
    from openscientist.webapp_components.pages.job_detail import report

    with patch.object(report, "ui") as ui:
        report._render_report_html_iframe(shared_job.job_dir)
    [props] = [c.args[0] for c in ui.element.return_value.props.call_args_list]
    url = props.split('src="', 1)[1].split('"', 1)[0]

    response = await request_as(shared_job.owner, url)
    assert response.status_code == 200
    assert "Water boils." in response.text


@pytest.mark.asyncio
async def test_a_job_without_a_report_frames_nothing_and_serves_nothing(
    shared_job: _SharedJob, request_as
) -> None:
    from openscientist.webapp_components.pages.job_detail import report

    (shared_job.job_dir / "final_report.html").unlink()
    with patch.object(report, "ui") as ui:
        report._render_report_html_iframe(shared_job.job_dir)
    ui.element.assert_not_called()

    response = await request_as(shared_job.owner, f"/web/jobs/{shared_job.job_id}/report.html")
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "viewer"])
async def test_the_zip_holds_no_chat_turn_files(
    shared_job: _SharedJob, request_as, who: str
) -> None:
    """A chat turn's exchange files leave the job directory mid-turn, so the
    archive built then must skip them for everyone who may see the job."""
    (shared_job.job_dir / ".chat_request_deadbeef.json").write_text('{"history": ["owner secret"]}')
    (shared_job.job_dir / ".chat_response_deadbeef.json").write_text('{"output": "owner secret"}')
    response = await request_as(
        getattr(shared_job, who), f"/web/jobs/{shared_job.job_id}/artifacts.zip"
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = zf.namelist()
    assert "provenance/plot.png" in names
    assert not any(n.startswith(".chat_") for n in names)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "name"),
    [
        ("owner", ".chat_request_deadbeef.json"),
        ("owner", ".chat_response_deadbeef.json"),
        ("viewer", ".chat_request_deadbeef.json"),
        ("viewer", ".chat_response_deadbeef.json"),
    ],
)
async def test_chat_turn_files_404_for_the_owner_and_the_recipient(
    shared_job: _SharedJob, request_as, who: str, name: str
) -> None:
    (shared_job.job_dir / name).write_text('{"history": ["owner secret"]}')
    response = await request_as(
        getattr(shared_job, who), f"/web/jobs/{shared_job.job_id}/files/{name}"
    )
    assert response.status_code == 404
    assert b"owner secret" not in response.content


@pytest_asyncio.fixture
async def public_job(db_session: AsyncSession, test_user: User, tmp_path: Path) -> _SharedJob:
    """A completed job of ``test_user`` made public, with a chat turn's exchange
    and an uploaded input in its directory."""
    reader = User(email=f"reader-{uuid4().hex}@files.test", name="Reader")
    job = Job(
        owner_id=test_user.id, research_question="Open to all?", status="completed", is_public=True
    )
    db_session.add_all([reader, job])
    await db_session.commit()
    await enable_rls(db_session)

    job_dir = tmp_path / str(job.id)
    (job_dir / "provenance").mkdir(parents=True)
    (job_dir / "provenance" / "plot.png").write_bytes(_PLOT)
    (job_dir / "data").mkdir()
    (job_dir / "data" / "upload.csv").write_text("owner-input")
    (job_dir / ".chat_request_ab12cd34.json").write_text('{"history": ["owner secret"]}')
    (job_dir / ".chat_response_ab12cd34.json").write_text('{"output": "owner secret"}')
    return _SharedJob(job.id, job_dir, test_user.id, test_user.id, reader.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "stranger"])
async def test_the_public_zip_holds_no_chat_turn_files(
    public_job: _SharedJob, db_session: AsyncSession, who: str
) -> None:
    """The reader who sees the job only because it is public gets the plot but
    neither the chat turn's exchange nor the owner's uploads."""
    user = getattr(public_job, who)

    class _Manager:
        jobs_dir = public_job.job_dir.parent

    app = FastAPI()

    async def session_as_user() -> AsyncIterator[AsyncSession]:
        await set_current_user(db_session, user)
        yield db_session

    app.dependency_overrides[get_session] = session_as_user
    app.dependency_overrides[get_current_user_id] = lambda: user
    app.include_router(router)
    with patch("openscientist.web_app.get_job_manager", return_value=_Manager()):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get(f"/web/jobs/{public_job.job_id}/artifacts.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = zf.namelist()
    assert "provenance/plot.png" in names
    assert not any(n.startswith(".chat_") for n in names)
    if who == "stranger":
        assert not any(n.startswith("data/") for n in names)


@pytest_asyncio.fixture
async def orphaned_job(db_session: AsyncSession, test_user2: User, tmp_path: Path) -> _SharedJob:
    """A completed job whose owner an admin deleted, so only the user row is
    gone; the job's files and a chat turn's exchange stay in its directory."""
    owner = User(email=f"deleted-{uuid4().hex}@files.test", name="Deleted")
    boss = User(email=f"boss-{uuid4().hex}@files.test", name="Boss")
    db_session.add_all([owner, boss])
    await db_session.flush()
    db_session.add(Administrator(user_id=boss.id))
    job = Job(owner_id=owner.id, research_question="Whose now?", status="completed")
    db_session.add(job)
    await db_session.commit()

    # The admin "delete user" action runs through the RLS-bypassing session and
    # jobs.owner_id is ON DELETE SET NULL, so the job survives without an owner.
    await db_session.delete(owner)
    await db_session.commit()

    job_dir = tmp_path / str(job.id)
    (job_dir / "provenance").mkdir(parents=True)
    (job_dir / "provenance" / "plot.png").write_bytes(_PLOT)
    (job_dir / ".chat_request_deadbeef.json").write_text('{"history": ["deleted owner secret"]}')
    (job_dir / ".chat_response_deadbeef.json").write_text('{"output": "deleted owner secret"}')
    return _SharedJob(job.id, job_dir, owner.id, boss.id, test_user2.id)


@pytest_asyncio.fixture
async def request_as_orphan(
    db_session: AsyncSession, orphaned_job: _SharedJob
) -> AsyncIterator[object]:
    """``request_as_orphan(user_id, path)`` as in ``request_as``, for the job
    whose owner is gone."""
    await enable_rls(db_session)

    class _Manager:
        jobs_dir = orphaned_job.job_dir.parent

    async def get(user_id: UUID | None, path: str) -> object:
        app = FastAPI()

        async def session_as_user() -> AsyncIterator[AsyncSession]:
            if user_id is not None:
                await set_current_user(db_session, user_id)
            yield db_session

        app.dependency_overrides[get_session] = session_as_user
        app.dependency_overrides[get_current_user_id] = lambda: user_id
        app.include_router(router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(path)

    with patch("openscientist.web_app.get_job_manager", return_value=_Manager()):
        yield get


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "files/provenance/plot.png",
        "artifacts.zip",
        "files/.chat_request_deadbeef.json",
        "files/.chat_response_deadbeef.json",
    ],
)
async def test_an_ownerless_job_serves_nothing_to_a_non_admin(
    orphaned_job: _SharedJob, request_as_orphan, path: str
) -> None:
    response = await request_as_orphan(
        orphaned_job.stranger, f"/web/jobs/{orphaned_job.job_id}/{path}"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_signed_in_admin_still_reads_the_ownerless_job(
    orphaned_job: _SharedJob, request_as_orphan
) -> None:
    response = await request_as_orphan(
        orphaned_job.viewer, f"/web/jobs/{orphaned_job.job_id}/files/provenance/plot.png"
    )
    assert response.status_code == 200
    assert response.content == _PLOT

    response = await request_as_orphan(
        orphaned_job.viewer, f"/web/jobs/{orphaned_job.job_id}/artifacts.zip"
    )
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = zf.namelist()
    assert "provenance/plot.png" in names
    assert not any(n.startswith(".chat_") for n in names)


@pytest.mark.asyncio
async def test_an_admin_assigns_the_ownerless_job_and_the_new_owner_reads_it(
    orphaned_job: _SharedJob, db_session: AsyncSession
) -> None:
    result = await assign_orphaned_job(db_session, orphaned_job.job_id, orphaned_job.stranger)
    assert result.ok
    assert result.reason == "assigned"

    await enable_rls(db_session)
    await set_current_user(db_session, orphaned_job.stranger)
    job = (
        await db_session.execute(select(Job).where(Job.id == orphaned_job.job_id))
    ).scalar_one_or_none()
    assert job is not None
    assert job.owner_id == orphaned_job.stranger
