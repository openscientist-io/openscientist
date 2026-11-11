"""Upload limits apply before multipart files are spooled."""

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from nicegui import app, ui
from nicegui.events import UploadEventArguments
from nicegui.testing import user_simulation
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette import formparsers

from openscientist import web_app
from tests.admin_row import set_admin_settings_row


async def _body(sizes: list[int]) -> AsyncIterator[bytes]:
    for index, size in enumerate(sizes):
        yield (
            f'--upload\r\nContent-Disposition: form-data; name="file{index}"; '
            f'filename="data{index}.csv"\r\nContent-Type: text/csv\r\n\r\n'
        ).encode()
        remaining = size
        while remaining:
            chunk = min(remaining, 65536)
            yield b"x" * chunk
            remaining -= chunk
        yield b"\r\n"
    yield b"--upload--\r\n"


def _host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    monkeypatch.setattr(web_app, "_state", web_app._AppState())
    monkeypatch.setattr(web_app, "_initialize_job_manager_runtime", lambda _: None)
    monkeypatch.setattr(web_app.ui, "run_with", lambda *args, **kwargs: None)
    for name in (
        "_register_oauth_routes",
        "_register_share_routes",
        "_register_download_routes",
        "_register_review_routes",
    ):
        monkeypatch.setattr(web_app, name, lambda: None)
    import_module = web_app.importlib.import_module
    monkeypatch.setattr(
        web_app.importlib,
        "import_module",
        lambda name, package=None: (
            None
            if name == "openscientist.webapp_components.pages"
            else import_module(name, package)
        ),
    )
    host = web_app.create_app(tmp_path / "jobs")
    host.mount("/", app)
    return host


@pytest.mark.parametrize("route", ["nicegui", "api"])
@pytest.mark.parametrize("sizes", [[2 * 1024 * 1024 + 1], [1048577, 1048576], [1] * 101])
@pytest.mark.parametrize("declared_length", [None, "1"])
async def test_upload_rejected_before_spooling_finishes(
    test_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    sizes: list[int],
    declared_length: str | None,
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=2)
    spool_dir = tmp_path / "spools"
    spool_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool_dir))
    opened: list[tempfile.SpooledTemporaryFile[bytes]] = []
    original_spool = formparsers.SpooledTemporaryFile

    def track_spool(*, max_size: int) -> tempfile.SpooledTemporaryFile[bytes]:
        spool = original_spool(max_size=max_size)
        opened.append(spool)
        return spool

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", track_spool)
    uploads: list[ui.upload] = []

    def root() -> None:
        uploads.append(ui.upload())

    async with user_simulation(root=root) as browser:
        host = _host(monkeypatch, tmp_path)
        if route == "api":
            # Exercise the API's multipart parser without creating a job.
            from openscientist.api.endpoints.jobs import _parse_job_create_request

            async def parse(request: Request) -> dict[str, bool]:
                await _parse_job_create_request(request)
                return {"accepted": True}

            host.router.routes.insert(0, APIRoute("/api/v1/jobs", parse, methods=["POST"]))
        await browser.open("/")
        path = uploads[0].props["url"] if route == "nicegui" else "/api/v1/jobs"
        headers = {"content-type": "multipart/form-data; boundary=upload"}
        if declared_length is not None:
            headers["content-length"] = declared_length
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(host), base_url="http://test"
        ) as client:
            response = await client.post(
                path,
                content=_body(sizes),
                headers=headers,
            )
        assert response.status_code == 413
        assert all(spool.closed for spool in opened)
        assert list(spool_dir.iterdir()) == []


async def test_content_length_rejected_without_reading_body(
    test_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    spool_dir = tmp_path / "spools"
    spool_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool_dir))

    def root() -> None:
        ui.upload()

    async def unread_body() -> AsyncIterator[bytes]:
        pytest.fail("Oversized Content-Length body was read")
        yield b""

    async with user_simulation(root=root):
        host = _host(monkeypatch, tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(host), base_url="http://test"
        ) as client:
            response = await client.post(
                "/_nicegui/client/unused/upload/1",
                content=unread_body(),
                headers={"content-length": str(3 * 1024 * 1024)},
            )
        assert response.status_code == 413
        assert list(spool_dir.iterdir()) == []


@pytest.mark.parametrize("size", [15, 1024 * 1024])
async def test_within_limit_upload_reaches_page_handler(
    test_engine: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    uploads: list[ui.upload] = []

    async def uploaded(event: UploadEventArguments) -> None:
        ui.label(f"Received {len(await event.file.read())} bytes")

    def root() -> None:
        uploads.append(ui.upload(on_upload=uploaded))

    async with user_simulation(root=root) as browser:
        host = _host(monkeypatch, tmp_path)
        await browser.open("/")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(host), base_url="http://test"
        ) as client:
            response = await client.post(
                uploads[0].props["url"], files={"file": ("data.csv", b"x" * size)}
            )
        assert response.status_code == 200
        await browser.should_see(f"Received {size} bytes")
