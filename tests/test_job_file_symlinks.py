"""The web process must not follow symlinks the agent plants in its job
directory. The agent container mounts the job directory read-write, so any
file in it can be replaced by a link pointing outside, and the web process
runs as root with the admin database password and provider keys in its
environment. Every web-side read, existence check and write of a job file
goes through openscientist.job_files, which refuses links at every path
component and serves and reads only the inode it opened.

The tests use the real database and the real routes; only the agent
container is faked, since the web side under test never spawns one here.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import openscientist.job_chat as job_chat
from openscientist.api.auth import get_current_user_from_api_key
from openscientist.api.router import api_router
from openscientist.database.models import Job, Session, User
from openscientist.database.rls import set_current_user, use_app_role
from openscientist.database.session import get_session
from openscientist.job import agent_error
from openscientist.job.types import JobInfo, JobStatus
from openscientist.webapp_components.artifact_routes import router as web_router
from openscientist.webapp_components.components import banners
from openscientist.webapp_components.pages.job_detail import iterations, report
from tests.helpers import enable_rls

_SECRET = b"SYMLINK-SECRET-PAYLOAD"


def _plant_secret(parent: Path, name: str = "secret.txt") -> Path:
    """A file outside the job directory holding a recognisable secret."""
    secret = parent / name
    secret.write_bytes(_SECRET)
    return secret


def _replace_with_symlink(target: Path, link_to: Path) -> None:
    """Swap ``target`` for a symlink to ``link_to``, as the agent would do."""
    link = target.with_name(target.name + ".linkswap")
    if link.exists() or link.is_symlink():
        os.unlink(link)
    os.symlink(link_to, link)
    os.replace(link, target)


@pytest_asyncio.fixture
async def job_db(db_session: AsyncSession, test_user: User) -> Job:
    job = Job(
        owner_id=test_user.id,
        research_question="Symlink probe",
        status="completed",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    await enable_rls(db_session)
    return job


@dataclass(frozen=True)
class _ApiJob:
    jobs_dir: Path
    job_dir: Path
    job: Job


@pytest_asyncio.fixture
async def api_job(job_db: Job, tmp_path: Path) -> _ApiJob:
    """A completed job whose directory the API tests plant symlinks in."""
    jobs_dir = tmp_path / "jobs"
    job_dir = jobs_dir / str(job_db.id)
    (job_dir / "provenance").mkdir(parents=True)
    return _ApiJob(jobs_dir, job_dir, job_db)


def _api_app(db_session: AsyncSession, user: User) -> FastAPI:
    app = FastAPI()

    async def override_get_session():
        await use_app_role(db_session)
        await set_current_user(db_session, user.id)
        yield db_session

    async def override_get_user():
        return user

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user_from_api_key] = override_get_user
    app.include_router(api_router)
    return app


@pytest_asyncio.fixture
async def web_job(test_engine: AsyncEngine, tmp_path: Path) -> AsyncIterator[tuple[User, str, Job]]:
    """A committed owner, the id of their live session (which the route's own
    admin login must see) and a completed job of theirs, so the route's real
    per-request sessions read the same rows the web's do."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"symlink-owner-{uuid4().hex}@symlink.test", name="Symlink Owner")
        db.add(owner)
        await db.flush()
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        job = Job(owner_id=owner.id, research_question="Web symlink probe", status="completed")
        db.add(job)
        await db.commit()
        await db.refresh(job)
    job_dir = tmp_path / "jobs" / str(job.id)
    (job_dir / "provenance").mkdir(parents=True)
    yield owner, str(login.id), job
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


def _web_client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _web_app(jobs_dir: Path) -> tuple[FastAPI, object]:
    """The artifact router on the app's real per-request sessions."""
    app = FastAPI()
    app.include_router(web_router)
    return app, SimpleNamespace(jobs_dir=jobs_dir)


class TestJobFilePrimitives:
    """The walk opens every component with O_NOFOLLOW, so a planted link at
    any level is refused and the opened fd pins the regular file."""

    def test_a_regular_file_is_opened_and_read(self, tmp_path: Path) -> None:
        from openscientist.job_files import open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "provenance").mkdir(parents=True)
        (job_dir / "provenance" / "plot.png").write_bytes(b"PNG-BYTES")

        with open_job_file(job_dir, "provenance/plot.png") as job_file:
            assert job_file.read_all() == b"PNG-BYTES"

    def test_a_leaf_symlink_is_refused(self, tmp_path: Path) -> None:
        from openscientist.job_files import JobFileError, open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "final_report.pdf").symlink_to(secret)

        with pytest.raises(JobFileError):
            open_job_file(job_dir, "final_report.pdf")
        assert secret.read_bytes() == _SECRET

    def test_a_symlinked_intermediate_directory_is_refused(self, tmp_path: Path) -> None:
        from openscientist.job_files import JobFileError, open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        (outside / "plot.png").write_bytes(b"OUTSIDE-PNG")
        job_dir.mkdir(parents=True)
        (job_dir / "provenance").symlink_to(outside, target_is_directory=True)

        with pytest.raises(JobFileError):
            open_job_file(job_dir, "provenance/plot.png")

    @pytest.mark.parametrize(
        "relative",
        ["", ".", "..", "../escape.txt", "a/../escape.txt", "/abs.txt", "a\\b.txt"],
    )
    def test_unsafe_relative_paths_are_refused(self, tmp_path: Path, relative: str) -> None:
        from openscientist.job_files import JobFileError, open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        (job_dir / "escape.txt").write_bytes(b"X")

        with pytest.raises(JobFileError):
            open_job_file(job_dir, relative)

    def test_a_missing_file_is_refused(self, tmp_path: Path) -> None:
        from openscientist.job_files import JobFileError, open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        with pytest.raises(JobFileError):
            open_job_file(job_dir, "missing.txt")

    def test_a_directory_leaf_is_refused(self, tmp_path: Path) -> None:
        from openscientist.job_files import JobFileError, open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "provenance").mkdir(parents=True)
        with pytest.raises(JobFileError):
            open_job_file(job_dir, "provenance")

    def test_the_size_cap_is_applied_on_the_opened_inode(self, tmp_path: Path) -> None:
        from openscientist.job_files import JobFileError, open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        (job_dir / "big.bin").write_bytes(b"x" * 64)

        with pytest.raises(JobFileError):
            open_job_file(job_dir, "big.bin", max_size=63)
        with open_job_file(job_dir, "big.bin", max_size=64) as job_file:
            assert job_file.size == 64

    def test_a_missing_job_directory_is_refused(self, tmp_path: Path) -> None:
        from openscientist.job_files import JobFileError, open_job_file

        with pytest.raises(JobFileError):
            open_job_file(tmp_path / "jobs" / "absent", "file.txt")

    def test_a_fifo_is_refused_without_blocking(self, tmp_path: Path) -> None:
        """A FIFO at the leaf would block a plain open until a writer appears;
        the walk must refuse it promptly instead of hanging a worker."""
        import threading

        from openscientist.job_files import JobFileError, open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        os.mkfifo(job_dir / "final_report.pdf")

        outcome: dict[str, bool] = {}

        def open_it() -> None:
            try:
                open_job_file(job_dir, "final_report.pdf")
                outcome["raised"] = False
            except JobFileError:
                outcome["raised"] = True

        worker = threading.Thread(target=open_it, daemon=True)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive(), "opening a FIFO must not block"
        assert outcome.get("raised") is True

    def test_the_opened_fd_pins_the_inode_across_a_late_symlink_swap(self, tmp_path: Path) -> None:
        """The file is swapped for a symlink after the safe open, and the
        handle must still read the original inode it pinned."""
        from openscientist.job_files import open_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        (job_dir / "final_report.md").write_bytes(b"ORIGINAL-REPORT")
        secret = _plant_secret(tmp_path)

        job_file = open_job_file(job_dir, "final_report.md")
        _replace_with_symlink(job_dir / "final_report.md", secret)
        try:
            assert job_file.read_all() == b"ORIGINAL-REPORT"
        finally:
            job_file.close()
        assert secret.read_bytes() == _SECRET

    def test_read_helpers_return_none_for_links_and_unsafe_paths(self, tmp_path: Path) -> None:
        from openscientist.job_files import read_job_file_bytes, read_job_file_text

        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "provenance").mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "final_report.pdf").symlink_to(secret)
        (job_dir / "provenance" / "plot.png").write_bytes(b"PNG")

        assert read_job_file_bytes(job_dir, "final_report.pdf") is None
        assert read_job_file_text(job_dir, "../escape") is None
        assert read_job_file_text(job_dir, "provenance/plot.png") == "PNG"
        assert read_job_file_text(job_dir, "provenance/plot.png", max_size=1) is None
        assert read_job_file_bytes(job_dir, "provenance/plot.png") == b"PNG"
        assert secret.read_bytes() == _SECRET

    def test_exists_is_built_on_the_same_walk(self, tmp_path: Path) -> None:
        from openscientist.job_files import job_file_exists

        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "provenance").mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "final_report.html").symlink_to(secret)
        (job_dir / "provenance" / "plot.png").write_bytes(b"PNG")

        assert job_file_exists(job_dir, "final_report.html") is False
        assert job_file_exists(job_dir, "provenance/plot.png") is True
        assert job_file_exists(job_dir, "missing.png") is False

    def test_list_files_skips_links_in_the_directory(self, tmp_path: Path) -> None:
        from openscientist.job_files import list_job_files

        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "provenance").mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "provenance" / "plot.png").write_bytes(b"PNG")
        (job_dir / "provenance" / "linked.png").symlink_to(secret)
        (job_dir / "provenance" / "linked-dir").symlink_to(tmp_path, target_is_directory=True)

        assert list_job_files(job_dir, "provenance") == ["plot.png"]
        assert list_job_files(job_dir, "provenance/linked-dir") == []
        assert list_job_files(job_dir, "missing") == []

    def test_write_replaces_a_planted_symlink_and_keeps_the_target(self, tmp_path: Path) -> None:
        from openscientist.job_files import write_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / ".chat_request_turn.json").symlink_to(secret)

        write_job_file(job_dir, ".chat_request_turn.json", '{"prompt": "hi"}')

        assert secret.read_bytes() == _SECRET
        assert not (job_dir / ".chat_request_turn.json").is_symlink()
        assert (job_dir / ".chat_request_turn.json").read_text() == '{"prompt": "hi"}'

    def test_write_overwrites_an_existing_regular_file(self, tmp_path: Path) -> None:
        from openscientist.job_files import write_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        (job_dir / "agent-error.txt").write_text("stale", encoding="utf-8")

        write_job_file(job_dir, "agent-error.txt", "fresh")

        assert (job_dir / "agent-error.txt").read_text() == "fresh"

    def test_install_replaces_a_planted_symlink_with_the_source(self, tmp_path: Path) -> None:
        from openscientist.job_files import install_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "final_report.pdf").symlink_to(secret)
        source = tmp_path / "source.pdf"
        source.write_bytes(b"GENERATED-PDF")

        install_file(job_dir, "final_report.pdf", source)

        assert secret.read_bytes() == _SECRET
        assert not (job_dir / "final_report.pdf").is_symlink()
        assert (job_dir / "final_report.pdf").read_bytes() == b"GENERATED-PDF"

    def test_a_write_into_a_symlinked_parent_dir_is_refused(self, tmp_path: Path) -> None:
        """A link at an intermediate component must not redirect the write:
        the staging and the rename stay inside the walked parent directory."""
        from openscientist.job_files import JobFileError, write_job_file

        job_dir = tmp_path / "jobs" / "j1"
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        job_dir.mkdir(parents=True)
        (job_dir / "sub").symlink_to(outside, target_is_directory=True)

        with pytest.raises(JobFileError):
            write_job_file(job_dir, "sub/note.txt", "hello")

        assert list(outside.iterdir()) == []

    def test_a_nested_write_into_real_dirs_still_lands(self, tmp_path: Path) -> None:
        from openscientist.job_files import write_job_file

        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "sub").mkdir(parents=True)

        write_job_file(job_dir, "sub/note.txt", "hello")

        assert (job_dir / "sub" / "note.txt").read_text() == "hello"

    def test_an_install_into_a_symlinked_parent_dir_is_refused(self, tmp_path: Path) -> None:
        from openscientist.job_files import JobFileError, install_file

        job_dir = tmp_path / "jobs" / "j1"
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        job_dir.mkdir(parents=True)
        (job_dir / "sub").symlink_to(outside, target_is_directory=True)
        source = tmp_path / "source.pdf"
        source.write_bytes(b"PDF")

        with pytest.raises(JobFileError):
            install_file(job_dir, "sub/final.pdf", source)

        assert list(outside.iterdir()) == []


class TestServeJobFile:
    """File responses stream the already-opened fd, so no path is ever
    reopened and no check-then-use gap remains."""

    @pytest.mark.asyncio
    async def test_the_response_streams_the_opened_fd_across_a_late_swap(
        self, tmp_path: Path
    ) -> None:
        from openscientist.job_files import open_job_file, serve_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        (job_dir / "final_report.pdf").write_bytes(b"ORIGINAL-PDF")
        secret = _plant_secret(tmp_path)

        app = FastAPI()

        @app.get("/report")
        def serve() -> object:
            job_file = open_job_file(job_dir, "final_report.pdf")
            _replace_with_symlink(job_dir / "final_report.pdf", secret)
            return serve_job_file(
                job_file, media_type="application/pdf", filename="r_final_report.pdf"
            )

        async with _web_client(app) as client:
            response = await client.get("/report")

        assert response.status_code == 200
        assert response.content == b"ORIGINAL-PDF"
        assert _SECRET not in response.content
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-length"] == str(len(b"ORIGINAL-PDF"))
        assert 'filename="r_final_report.pdf"' in response.headers["content-disposition"]
        assert secret.read_bytes() == _SECRET

    @pytest.mark.asyncio
    async def test_the_response_keeps_caller_headers(self, tmp_path: Path) -> None:
        from openscientist.job_files import open_job_file, serve_job_file

        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        (job_dir / "plot.png").write_bytes(b"PNG")

        app = FastAPI()

        @app.get("/plot")
        def serve() -> object:
            # the fd stays open for the response; serve_job_file closes it
            job_file = open_job_file(job_dir, "plot.png")
            return serve_job_file(
                job_file,
                media_type="image/png",
                headers={
                    "Content-Security-Policy": "sandbox",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        async with _web_client(app) as client:
            response = await client.get("/plot")

        assert response.status_code == 200
        assert response.content == b"PNG"
        assert response.headers["content-security-policy"] == "sandbox"
        assert response.headers["x-content-type-options"] == "nosniff"


class TestApiReportRoute:
    """GET /api/v1/jobs/{id}/report must not read, write through or serve a
    planted link, and must keep serving regular files unchanged."""

    @pytest.mark.asyncio
    async def test_a_symlinked_pdf_is_not_served(
        self, db_session: AsyncSession, test_user: User, api_job: _ApiJob
    ) -> None:
        secret = _plant_secret(api_job.jobs_dir.parent)
        _replace_with_symlink(api_job.job_dir / "final_report.pdf", secret)

        app = _api_app(db_session, test_user)
        with patch("openscientist.api.endpoints.jobs._get_jobs_dir", return_value=api_job.jobs_dir):
            async with _web_client(app) as client:
                response = await client.get(f"/api/v1/jobs/{api_job.job.id}/report")

        assert response.status_code == 404
        assert _SECRET not in response.content
        assert secret.read_bytes() == _SECRET

    @pytest.mark.asyncio
    async def test_a_symlinked_markdown_falls_back_to_the_saved_html(
        self, db_session: AsyncSession, test_user: User, api_job: _ApiJob
    ) -> None:
        secret = _plant_secret(api_job.jobs_dir.parent)
        _replace_with_symlink(api_job.job_dir / "final_report.md", secret)
        (api_job.job_dir / "final_report.html").write_text(
            "<html><body>Saved HTML report</body></html>"
        )

        app = _api_app(db_session, test_user)
        with patch("openscientist.api.endpoints.jobs._get_jobs_dir", return_value=api_job.jobs_dir):
            async with _web_client(app) as client:
                response = await client.get(f"/api/v1/jobs/{api_job.job.id}/report")

        assert response.status_code == 200
        assert b"Saved HTML report" in response.content
        assert _SECRET not in response.content
        assert secret.read_bytes() == _SECRET

    @pytest.mark.asyncio
    async def test_a_symlinked_pdf_is_replaced_by_a_generated_one(
        self, db_session: AsyncSession, test_user: User, api_job: _ApiJob
    ) -> None:
        """The PDF generation branch must write the rendered PDF into the job
        directory without following a planted link, and then serve it. Links
        at both names the branch used to write (_final_report_stripped.md and
        final_report.pdf) must leave their targets untouched."""
        secret = _plant_secret(api_job.jobs_dir.parent)
        _replace_with_symlink(api_job.job_dir / "final_report.pdf", secret)
        (api_job.job_dir / "_final_report_stripped.md").symlink_to(secret)
        (api_job.job_dir / "final_report.md").write_text("# Visible\n\nBody text.\n")

        app = _api_app(db_session, test_user)
        with patch("openscientist.api.endpoints.jobs._get_jobs_dir", return_value=api_job.jobs_dir):
            async with _web_client(app) as client:
                response = await client.get(f"/api/v1/jobs/{api_job.job.id}/report")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert _SECRET not in response.content
        assert secret.read_bytes() == _SECRET
        generated = api_job.job_dir / "final_report.pdf"
        assert generated.is_file() and not generated.is_symlink()
        assert generated.read_bytes().startswith(b"%PDF")
        # The branch no longer writes _final_report_stripped.md into the job
        # directory at all, so the planted link was never touched.
        assert (api_job.job_dir / "_final_report_stripped.md").is_symlink()

    @pytest.mark.asyncio
    async def test_a_regular_pdf_is_served_unchanged(
        self, db_session: AsyncSession, test_user: User, api_job: _ApiJob
    ) -> None:
        pdf_bytes = b"%PDF-1.4 original report bytes"
        (api_job.job_dir / "final_report.pdf").write_bytes(pdf_bytes)

        app = _api_app(db_session, test_user)
        with patch("openscientist.api.endpoints.jobs._get_jobs_dir", return_value=api_job.jobs_dir):
            async with _web_client(app) as client:
                response = await client.get(f"/api/v1/jobs/{api_job.job.id}/report")

        assert response.status_code == 200
        assert response.content == pdf_bytes
        assert response.headers["content-type"] == "application/pdf"
        assert "Symlink_probe_final_report.pdf" in response.headers["content-disposition"]


class TestWebArtifactRoutes:
    """The session routes /web/jobs/{id}/files/... and /report.html must not
    read or serve a planted link, and keep serving regular files."""

    @pytest.mark.asyncio
    async def test_a_symlinked_figure_is_not_served(
        self, web_job: tuple[User, str, Job], tmp_path: Path
    ) -> None:
        _, token, job = web_job
        app, manager = _web_app(tmp_path / "jobs")
        job_dir = tmp_path / "jobs" / str(job.id)
        secret = _plant_secret(tmp_path)
        _replace_with_symlink(job_dir / "provenance" / "plot.png", secret)

        with patch("openscientist.web_app.get_job_manager", return_value=manager):
            async with _web_client(app) as client:
                client.cookies.set("session_token", token)
                response = await client.get(f"/web/jobs/{job.id}/files/provenance/plot.png")

        assert response.status_code == 404
        assert _SECRET not in response.content

    @pytest.mark.asyncio
    async def test_a_symlinked_intermediate_directory_is_not_traversed(
        self, web_job: tuple[User, str, Job], tmp_path: Path
    ) -> None:
        _, token, job = web_job
        app, manager = _web_app(tmp_path / "jobs")
        job_dir = tmp_path / "jobs" / str(job.id)
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        (outside / "secret.txt").write_bytes(_SECRET)
        (job_dir / "provenance" / "linked-sub").symlink_to(outside, target_is_directory=True)

        with patch("openscientist.web_app.get_job_manager", return_value=manager):
            async with _web_client(app) as client:
                client.cookies.set("session_token", token)
                response = await client.get(
                    f"/web/jobs/{job.id}/files/provenance/linked-sub/secret.txt"
                )

        assert response.status_code == 404
        assert _SECRET not in response.content

    @pytest.mark.asyncio
    async def test_a_fifo_at_a_served_name_404s_without_hanging(
        self, web_job: tuple[User, str, Job], tmp_path: Path
    ) -> None:
        """The agent can mkfifo a served name; the route must answer 404
        promptly instead of hanging a worker until a writer appears."""
        _, token, job = web_job
        app, manager = _web_app(tmp_path / "jobs")
        job_dir = tmp_path / "jobs" / str(job.id)
        os.mkfifo(job_dir / "provenance" / "plot.png")

        with patch("openscientist.web_app.get_job_manager", return_value=manager):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                timeout=httpx.Timeout(5),
            ) as client:
                client.cookies.set("session_token", token)
                response = await client.get(f"/web/jobs/{job.id}/files/provenance/plot.png")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_a_regular_file_is_served_with_sandbox_headers(
        self, web_job: tuple[User, str, Job], tmp_path: Path
    ) -> None:
        _, token, job = web_job
        app, manager = _web_app(tmp_path / "jobs")
        job_dir = tmp_path / "jobs" / str(job.id)
        (job_dir / "provenance" / "plot.png").write_bytes(b"PNG-BYTES")

        with patch("openscientist.web_app.get_job_manager", return_value=manager):
            async with _web_client(app) as client:
                client.cookies.set("session_token", token)
                response = await client.get(f"/web/jobs/{job.id}/files/provenance/plot.png")

        assert response.status_code == 200
        assert response.content == b"PNG-BYTES"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in response.headers["content-security-policy"]

    @pytest.mark.asyncio
    async def test_a_symlinked_report_html_is_not_read_back(
        self, web_job: tuple[User, str, Job], tmp_path: Path
    ) -> None:
        _, token, job = web_job
        app, manager = _web_app(tmp_path / "jobs")
        job_dir = tmp_path / "jobs" / str(job.id)
        secret = _plant_secret(tmp_path)
        _replace_with_symlink(job_dir / "final_report.html", secret)
        # no final_report.md, so the route has nothing else to fall back on

        with patch("openscientist.web_app.get_job_manager", return_value=manager):
            async with _web_client(app) as client:
                client.cookies.set("session_token", token)
                response = await client.get(f"/web/jobs/{job.id}/report.html")

        assert response.status_code == 404
        assert _SECRET not in response.content

    @pytest.mark.asyncio
    async def test_a_symlinked_markdown_falls_back_to_the_saved_html(
        self, web_job: tuple[User, str, Job], tmp_path: Path
    ) -> None:
        _, token, job = web_job
        app, manager = _web_app(tmp_path / "jobs")
        job_dir = tmp_path / "jobs" / str(job.id)
        secret = _plant_secret(tmp_path)
        _replace_with_symlink(job_dir / "final_report.md", secret)
        (job_dir / "final_report.html").write_text("<html><body>Saved HTML</body></html>")

        with patch("openscientist.web_app.get_job_manager", return_value=manager):
            async with _web_client(app) as client:
                client.cookies.set("session_token", token)
                response = await client.get(f"/web/jobs/{job.id}/report.html")

        assert response.status_code == 200
        assert "Saved HTML" in response.text
        assert "SYMLINK-SECRET-PAYLOAD" not in response.text

    @pytest.mark.asyncio
    async def test_a_symlinked_figure_is_never_embedded(
        self, web_job: tuple[User, str, Job], tmp_path: Path
    ) -> None:
        _, token, job = web_job
        app, manager = _web_app(tmp_path / "jobs")
        job_dir = tmp_path / "jobs" / str(job.id)
        secret = _plant_secret(tmp_path, name="secret.png")
        _replace_with_symlink(job_dir / "provenance" / "plot.png", secret)
        (job_dir / "final_report.md").write_text(
            "# Report\n\n{{figure:plot.png|caption=The plot}}\n"
        )
        (job_dir / "final_report.html").write_text("<html><body>shell</body></html>")

        with patch("openscientist.web_app.get_job_manager", return_value=manager):
            async with _web_client(app) as client:
                client.cookies.set("session_token", token)
                response = await client.get(f"/web/jobs/{job.id}/report.html")

        import base64

        assert response.status_code == 200
        assert _SECRET not in response.content
        assert base64.b64encode(_SECRET).decode("ascii") not in response.text

    @pytest.mark.asyncio
    async def test_a_symlinked_provenance_directory_embeds_nothing(
        self, web_job: tuple[User, str, Job], tmp_path: Path
    ) -> None:
        _, token, job = web_job
        app, manager = _web_app(tmp_path / "jobs")
        job_dir = tmp_path / "jobs" / str(job.id)
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        (outside / "plot.png").write_bytes(_SECRET)
        os.rmdir(job_dir / "provenance")
        (job_dir / "provenance").symlink_to(outside, target_is_directory=True)
        (job_dir / "final_report.md").write_text(
            "# Report\n\n{{figure:plot.png|caption=The plot}}\n"
        )
        (job_dir / "final_report.html").write_text("<html><body>shell</body></html>")

        with patch("openscientist.web_app.get_job_manager", return_value=manager):
            async with _web_client(app) as client:
                client.cookies.set("session_token", token)
                response = await client.get(f"/web/jobs/{job.id}/report.html")

        assert response.status_code == 200
        assert _SECRET not in response.content


class TestUiReportPage:
    """The NiceGUI report tab reads and writes job files in the web process."""

    async def test_a_symlinked_pdf_is_not_downloaded(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "job-1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "final_report.md").write_text("# Visible\n\nBody text.\n")
        (job_dir / "final_report.pdf").symlink_to(secret)

        def fake_markdown_to_pdf(source, target, run_environment=None) -> None:
            target.write_bytes(b"PDF-GEN")

        with (
            patch.object(report, "ui") as ui,
            patch.object(report, "markdown_to_pdf", side_effect=fake_markdown_to_pdf),
        ):
            await report._download_pdf_report(
                job_dir / "final_report.md", job_dir / "final_report.pdf", "job-1", None
            )

        assert secret.read_bytes() == _SECRET
        assert not (job_dir / "_final_report_stripped.md").exists()
        generated = job_dir / "final_report.pdf"
        assert generated.is_file() and not generated.is_symlink()
        assert generated.read_bytes() == b"PDF-GEN"
        ui.download.assert_called_once_with(b"PDF-GEN", filename="job-1_report.pdf")

    async def test_a_symlinked_markdown_fails_without_writing_through(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "job-1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "final_report.md").symlink_to(secret)

        def fake_markdown_to_pdf(source, target, run_environment=None) -> None:
            target.write_bytes(b"PDF-GEN")

        with (
            patch.object(report, "ui") as ui,
            patch.object(report, "markdown_to_pdf", side_effect=fake_markdown_to_pdf),
        ):
            await report._download_pdf_report(
                job_dir / "final_report.md", job_dir / "final_report.pdf", "job-1", None
            )

        assert secret.read_bytes() == _SECRET
        ui.download.assert_not_called()
        ui.notify.assert_called()

    def test_the_markdown_view_refuses_a_symlinked_report(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "job-1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "final_report.md").symlink_to(secret)

        with patch.object(report, "ui") as ui:
            report._render_report_markdown(job_dir / "final_report.md")

        ui.markdown.assert_not_called()

    def test_the_report_iframe_refuses_a_symlinked_html(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "job-1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "final_report.html").symlink_to(secret)

        with patch.object(report, "ui") as ui:
            report._render_report_html_iframe(job_dir)

        ui.element.assert_not_called()


class TestIterationPlots:
    """The plot inventory and its Download button run in the web process."""

    def test_a_symlinked_plot_is_not_listed(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "provenance").mkdir(parents=True)
        secret = _plant_secret(tmp_path, name="secret.png")
        (job_dir / "provenance" / "plot.png").symlink_to(secret)
        (job_dir / "provenance" / "plot.json").write_text(
            json.dumps({"iteration": 1, "description": "d"})
        )

        plots = iterations._collect_iteration_plots(job_dir / "provenance", 1)

        assert plots == []

    def test_a_symlinked_metadata_file_is_not_listed(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "provenance").mkdir(parents=True)
        secret = _plant_secret(tmp_path, name="secret.json")
        secret.write_text(json.dumps({"iteration": 1, "description": "S"}))
        (job_dir / "provenance" / "plot.png").write_bytes(b"PNG")
        (job_dir / "provenance" / "plot.json").symlink_to(secret)

        plots = iterations._collect_iteration_plots(job_dir / "provenance", 1)

        assert plots == []

    def test_a_regular_plot_with_metadata_is_listed(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "j1"
        (job_dir / "provenance").mkdir(parents=True)
        (job_dir / "provenance" / "plot.png").write_bytes(b"PNG")
        (job_dir / "provenance" / "plot.json").write_text(
            json.dumps({"iteration": 1, "description": "d"})
        )

        plots = iterations._collect_iteration_plots(job_dir / "provenance", 1)

        assert [path for path, _ in plots] == [job_dir / "provenance" / "plot.png"]

    def test_the_download_button_refuses_a_late_symlink_swap(self, tmp_path: Path) -> None:
        """The plot is a regular file when the page renders, and a link when
        the button is clicked: the click must not download the target."""
        job_dir = tmp_path / "jobs" / "j1"
        provenance = job_dir / "provenance"
        provenance.mkdir(parents=True)
        plot = provenance / "plot.png"
        plot.write_bytes(b"PNG")
        (provenance / "plot.json").write_text(json.dumps({"iteration": 1}))
        secret = _plant_secret(tmp_path, name="secret.png")

        with patch.object(iterations, "ui") as ui:
            iterations._render_iteration_plots(provenance, 1)
        button = [
            call for call in ui.button.call_args_list if call.args and call.args[0] == "Download"
        ][0]

        _replace_with_symlink(plot, secret)
        button.kwargs["on_click"]()

        ui.download.assert_not_called()
        assert secret.read_bytes() == _SECRET


class TestChatTurnFiles:
    """The web side of a chat turn writes the request and reads the reply in
    the job directory, where the agent can plant links."""

    async def test_a_planted_link_at_the_request_name_is_not_written_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_dir = tmp_path / "jobs" / "j-chat"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        fixed_turn = UUID(int=0xABC)
        monkeypatch.setattr(job_chat, "uuid4", lambda: fixed_turn)
        request_name, _ = job_chat._chat_turn_files(job_dir, fixed_turn.hex)
        request_name.symlink_to(secret)

        request_seen: list[str] = []

        class _Runner:
            def run_chat_turn(self, job_id: str, job_dir: Path, chat_turn: str) -> None:
                request, response = job_chat._chat_turn_files(Path(job_dir), chat_turn)
                request_seen.append(request.read_text(encoding="utf-8"))
                response.write_text(json.dumps({"output": "hello"}), encoding="utf-8")

        async def _fake_request(session, job_id, message):
            return {"system_prompt": "SP", "model_override": None, "prompt": message}

        session = MagicMock(spec=AsyncSession)
        with (
            patch("openscientist.job_container.runner.JobContainerRunner", return_value=_Runner()),
            patch.object(job_chat, "_build_chat_request", _fake_request),
        ):
            reply = await job_chat._send_message_via_container(session, UUID(int=1), "hi", job_dir)

        assert reply == "hello"
        assert secret.read_bytes() == _SECRET
        assert json.loads(request_seen[0])["prompt"] == "hi"

    async def test_a_planted_link_at_the_response_name_is_not_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job_dir = tmp_path / "jobs" / "j-chat"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path, name="secret.json")
        secret.write_text(json.dumps({"output": "SECRET"}), encoding="utf-8")
        fixed_turn = UUID(int=0xABC)
        monkeypatch.setattr(job_chat, "uuid4", lambda: fixed_turn)
        _, response_name = job_chat._chat_turn_files(job_dir, fixed_turn.hex)
        response_name.symlink_to(secret)

        class _Runner:
            def run_chat_turn(self, job_id: str, job_dir: Path, chat_turn: str) -> None:
                pass

        async def _fake_request(session, job_id, message):
            return {"system_prompt": "SP", "model_override": None, "prompt": message}

        session = MagicMock(spec=AsyncSession)
        with (
            patch("openscientist.job_container.runner.JobContainerRunner", return_value=_Runner()),
            patch.object(job_chat, "_build_chat_request", _fake_request),
        ):
            with pytest.raises(RuntimeError, match="no response"):
                await job_chat._send_message_via_container(session, UUID(int=1), "hi", job_dir)

        assert secret.read_text() == json.dumps({"output": "SECRET"})
        assert not response_name.exists()


class TestAgentError:
    def test_a_symlinked_error_file_is_not_read(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "agent-error.txt").symlink_to(secret)

        assert agent_error.take_agent_error(job_dir) is None

        assert not (job_dir / "agent-error.txt").exists()
        assert secret.read_bytes() == _SECRET

    def test_a_regular_error_file_is_still_taken(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        (job_dir / "agent-error.txt").write_text("boom", encoding="utf-8")

        assert agent_error.take_agent_error(job_dir) == "boom"
        assert not (job_dir / "agent-error.txt").exists()


class TestErrorBanner:
    def _error_info(self) -> dict:
        return {
            "category": "configuration",
            "title": "Test Error",
            "message": "Test message",
            "extracted_error": "Error details",
            "steps": ["Step 1"],
            "raw": "Raw error",
            "contact_admin": True,
        }

    def _job_info(self) -> JobInfo:
        return JobInfo(
            job_id="j1",
            research_question="Q?",
            status=JobStatus.FAILED,
            created_at="2026-02-05T09:00:00",
            failed_at="2026-02-05T10:00:00",
            max_iterations=5,
            iterations_completed=1,
        )

    def _labels(self, ui_mock) -> list[str]:
        return [
            call.args[0]
            for call in ui_mock.label.call_args_list
            if call.args and isinstance(call.args[0], str)
        ]

    def test_the_log_hint_shows_for_a_regular_log(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        (job_dir / "orchestrator.log").write_text("log", encoding="utf-8")

        with patch.object(banners, "ui") as ui:
            banners.render_error_card(self._error_info(), self._job_info(), job_dir)

        assert any("orchestrator.log" in label for label in self._labels(ui))

    def test_the_log_hint_is_hidden_for_a_symlinked_log(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "jobs" / "j1"
        job_dir.mkdir(parents=True)
        secret = _plant_secret(tmp_path)
        (job_dir / "orchestrator.log").symlink_to(secret)

        with patch.object(banners, "ui") as ui:
            banners.render_error_card(self._error_info(), self._job_info(), job_dir)

        assert not any("orchestrator.log" in label for label in self._labels(ui))
