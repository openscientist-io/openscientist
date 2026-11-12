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
from openscientist.upload_limits import UploadLimitsMiddleware
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


@pytest.mark.parametrize(
    ("declared_length", "status", "detail"),
    [
        (str(3 * 1024 * 1024), 413, "Upload body exceeds the size limit"),
        ("invalid", 400, "Invalid Content-Length"),
    ],
)
async def test_content_length_rejected_without_reading_body(
    test_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_length: str,
    status: int,
    detail: str,
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    spool_dir = tmp_path / "spools"
    spool_dir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spool_dir))

    def root() -> None:
        ui.upload()

    async def unread_body() -> AsyncIterator[bytes]:
        pytest.fail("Rejected Content-Length body was read")
        yield b""

    async with user_simulation(root=root):
        host = _host(monkeypatch, tmp_path)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(host), base_url="http://test"
        ) as client:
            response = await client.post(
                "/_nicegui/client/unused/upload/1",
                content=unread_body(),
                headers={"content-length": declared_length},
            )
        assert response.status_code == status
        assert response.json() == {"detail": detail}
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


@pytest.mark.parametrize("declared_length", [None, "1"])
@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_streamed_body_limit(
    test_engine: AsyncEngine, declared_length: str | None, extra_bytes: int
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    host = FastAPI()
    host.add_middleware(UploadLimitsMiddleware)
    body_limit = 2 * 1024 * 1024

    @host.post("/api/v1/jobs")
    async def read_body(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    async def body() -> AsyncIterator[bytes]:
        for _ in range(body_limit // 65536):
            yield b"x" * 65536
        if extra_bytes:
            yield b"x" * extra_bytes

    headers = {} if declared_length is None else {"content-length": declared_length}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(host), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/jobs", content=body(), headers=headers)
    if extra_bytes:
        assert response.status_code == 413
        assert response.json() == {"detail": "Upload body exceeds the size limit"}
    else:
        assert response.status_code == 200
        assert response.json() == {"size": body_limit}


async def test_downstream_error_propagates_without_upload_rejection(
    test_engine: AsyncEngine,
) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    host = FastAPI()
    host.add_middleware(UploadLimitsMiddleware)
    error = RuntimeError("Upload handler failed")

    @host.post("/api/v1/jobs")
    async def fail(request: Request) -> None:
        assert await request.body() == b"data"
        raise error

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(host), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError, match="Upload handler failed") as raised:
            await client.post("/api/v1/jobs", content=b"data")
    assert raised.value is error


async def test_upload_id_beyond_integer_conversion_limit(test_engine: AsyncEngine) -> None:
    await set_admin_settings_row(test_engine, max_file_size_mb=1)
    uploads: list[ui.upload] = []

    def root() -> None:
        uploads.append(ui.upload())

    async with user_simulation(root=root) as browser:
        host = FastAPI()
        host.add_middleware(UploadLimitsMiddleware)

        @host.post("/_nicegui/client/{client_id}/upload/{upload_id}")
        async def receive_upload(request: Request) -> dict[str, bool]:
            assert await request.body()
            return {"accepted": True}

        await browser.open("/")
        path = uploads[0].props["url"].rsplit("/", 1)[0] + "/" + "9" * 5000
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(host), base_url="http://test"
        ) as client:
            response = await client.post(path, files={"file": ("data.csv", b"a,b\n1,2\n")})
        assert response.status_code == 200
        assert response.json() == {"accepted": True}
