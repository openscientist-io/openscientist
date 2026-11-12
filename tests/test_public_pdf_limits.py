"""Public PDF requests bound renderer resources and keep the web responsive."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import BinaryIO

import httpx
import pymupdf  # type: ignore[import-untyped]
import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from PIL import Image

from openscientist.report import pdf
from openscientist.report.download import report_download_response
from openscientist.settings import clear_settings_cache


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture
def renderers(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.subprocess.Process]:
    processes: list[asyncio.subprocess.Process] = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(
        *args: str, stdin: int, stdout: BinaryIO, stderr: int | BinaryIO, start_new_session: bool
    ) -> asyncio.subprocess.Process:
        process = await real_spawn(
            *args, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=start_new_session
        )
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return processes


@pytest.fixture
async def reader(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()

    @app.get("/report")
    async def report() -> Response:
        return await report_download_response(tmp_path, "report", public_reader=True)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def write_table(job_dir: Path, rows: int) -> None:
    row = "<tr>" + "<td>Long wrapping table cell content</td>" * 20 + "</tr>"
    (job_dir / "final_report.md").write_text(
        "# Report\n\n<table>" + row * rows + "</table>", encoding="utf-8"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("rows", "deadline"), [(1500, 1.0), (1500, 3.0)])
async def test_pathological_report_deadline_keeps_web_responsive(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: int,
    deadline: float,
    renderers: list[asyncio.subprocess.Process],
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_PUBLIC_PDF_TIMEOUT_SECONDS", str(deadline))
    write_table(tmp_path, rows)
    started = time.monotonic()
    request = asyncio.create_task(reader.get("/report"))
    await asyncio.sleep(1 if deadline > 1 else 0.02)
    health = await asyncio.wait_for(reader.get("/health"), timeout=1)
    response = await asyncio.wait_for(request, timeout=30)

    assert health.status_code == 200
    assert response.status_code == 504
    assert "too long" in response.json()["detail"]
    assert time.monotonic() - started < deadline + 2
    assert not (tmp_path / "final_report.pdf").exists()
    assert len(renderers) == 1
    assert renderers[0].returncode == -signal.SIGKILL
    with pytest.raises(ProcessLookupError):
        os.kill(renderers[0].pid, 0)


@pytest.mark.asyncio
async def test_public_report_concurrency_is_shared_between_requests(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_PUBLIC_PDF_MAX_CONCURRENT", "1")
    monkeypatch.setenv("OPENSCIENTIST_PUBLIC_PDF_TIMEOUT_SECONDS", "3")
    write_table(tmp_path, 1500)
    first = asyncio.create_task(reader.get("/report"))
    await asyncio.sleep(0.2)
    try:
        second = await asyncio.wait_for(reader.get("/report"), timeout=1)
        assert second.status_code == 503
        assert "busy" in second.json()["detail"]
    finally:
        await asyncio.wait_for(first, timeout=10)

    (tmp_path / "final_report.md").write_text("# Short report\n", encoding="utf-8")
    monkeypatch.setenv("OPENSCIENTIST_PUBLIC_PDF_TIMEOUT_SECONDS", "30")
    clear_settings_cache()
    assert (await reader.get("/report")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting", "value"),
    [("OPENSCIENTIST_PUBLIC_PDF_CPU_SECONDS", "1"), ("OPENSCIENTIST_PUBLIC_PDF_MEMORY_MB", "128")],
)
async def test_public_report_resource_limit_reaches_reader(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    value: str,
) -> None:
    monkeypatch.setenv(setting, value)
    if setting.endswith("MEMORY_MB"):
        (tmp_path / "final_report.md").write_text("# Normal report\n")
        (tmp_path / "sitecustomize.py").write_text(
            "import builtins\n"
            "original_import = builtins.__import__\n"
            "def allocate_in_renderer(name, *args, **kwargs):\n"
            "    if name == 'openscientist.report.pdf':\n"
            "        bytearray(256 * 1024 * 1024)\n"
            "    return original_import(name, *args, **kwargs)\n"
            "builtins.__import__ = allocate_in_renderer\n"
        )
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    else:
        write_table(tmp_path, 1500)
    response = await asyncio.wait_for(reader.get("/report"), timeout=15)
    assert response.status_code == 422
    assert "rendering limits" in response.json()["detail"]


@pytest.mark.asyncio
async def test_isolated_public_report_keeps_figures_without_uploaded_images(
    reader: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    for directory in ("data", "previews", "provenance"):
        (tmp_path / directory).mkdir()
    Image.new("RGB", (13, 17), "red").save(tmp_path / "data" / "upload.png")
    Image.new("RGB", (11, 15), "green").save(tmp_path / "previews" / "upload.png")
    Image.new("RGB", (19, 23), "blue").save(tmp_path / "provenance" / "figure.png")
    (tmp_path / "final_report.md").write_text(
        "# Report\n\n![Input](data/upload.png)\n\n![Preview](previews/upload.png)\n\n"
        "{{figure:figure.png|caption=Generated figure}}\n",
        encoding="utf-8",
    )
    response = await reader.get("/report")
    assert response.status_code == 200
    with pymupdf.open(stream=response.content, filetype="pdf") as document:
        image_sizes = {(image[2], image[3]) for page in document for image in page.get_images()}
    assert image_sizes == {(19, 23)}
    assert not (tmp_path / "final_report.pdf").exists()


@pytest.mark.asyncio
async def test_cancelled_public_report_kills_renderer_and_releases_capacity(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    renderers: list[asyncio.subprocess.Process],
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_PUBLIC_PDF_MAX_CONCURRENT", "1")
    write_table(tmp_path, 1500)
    request = asyncio.create_task(reader.get("/report"))
    await asyncio.sleep(0.2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, timeout=2)
    assert renderers[0].returncode == -signal.SIGKILL
    with pytest.raises(ProcessLookupError):
        os.kill(renderers[0].pid, 0)
    (tmp_path / "final_report.md").write_text("# Short report\n", encoding="utf-8")
    assert (await reader.get("/report")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["missing", "symlink", "oversized"])
async def test_unavailable_markdown_precedes_renderer_limits(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_PUBLIC_PDF_MEMORY_MB", "32")
    (tmp_path / "final_report.pdf").write_bytes(b"private cached report")
    markdown = tmp_path / "final_report.md"
    if source == "symlink":
        private = tmp_path / "private.md"
        private.write_text("# Private input\n")
        markdown.symlink_to(private)
    elif source == "oversized":
        with markdown.open("wb") as report:
            report.truncate(50 * 1024 * 1024 + 1)

    response = await reader.get("/report")

    assert response.status_code == 404
    assert b"private cached report" not in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        ("ImportError('worker dependency unavailable')", 500),
        ("OSError(errno.EIO, 'worker dependency unavailable')", 500),
        ("OSError(errno.ENOMEM, 'worker dependency unavailable')", 422),
        ("OSError(errno.EFBIG, 'worker dependency unavailable')", 422),
    ],
)
async def test_worker_import_failure_reaches_reader(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: str,
    status_code: int,
) -> None:
    (tmp_path / "final_report.md").write_text("# Normal report\n")
    (tmp_path / "sitecustomize.py").write_text(
        "import builtins\n"
        "import errno\n"
        "original_import = builtins.__import__\n"
        "def fail_renderer_import(name, *args, **kwargs):\n"
        "    if name == 'openscientist.report.pdf':\n"
        f"        raise {error}\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = fail_renderer_import\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))

    response = await reader.get("/report")

    assert response.status_code == status_code
    if status_code == 500:
        assert "worker dependency unavailable" in caplog.text
    else:
        assert "rendering limits" in response.json()["detail"]
    assert "worker dependency unavailable" not in response.text


@pytest.mark.asyncio
async def test_exited_renderer_cleanup_is_harmless(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "final_report.md").write_text("# Normal report\n")

    def already_exited(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", already_exited)

    response = await reader.get("/report")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_markdown_removed_before_worker_starts_returns_not_found(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markdown = tmp_path / "final_report.md"
    markdown.write_text("# Normal report\n")
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(
        *args: str, stdin: int, stdout: BinaryIO, stderr: int | BinaryIO, start_new_session: bool
    ) -> asyncio.subprocess.Process:
        markdown.unlink()
        return await real_spawn(
            *args, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=start_new_session
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    response = await asyncio.wait_for(reader.get("/report"), timeout=30)

    assert response.status_code == 404
    assert response.json()["detail"] == "Report not found"


@pytest.mark.asyncio
async def test_parent_rejects_pdf_larger_than_output_limit(
    reader: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "final_report.md").write_text("# Normal report\n")
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(
        *args: str, stdin: int, stdout: BinaryIO, stderr: int | BinaryIO, start_new_session: bool
    ) -> asyncio.subprocess.Process:
        process = await real_spawn(
            *args, stdin=stdin, stdout=stdout, stderr=stderr, start_new_session=start_new_session
        )
        monkeypatch.setattr(pdf, "MAX_ARTIFACT_FILE_SIZE_BYTES", 1)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    response = await asyncio.wait_for(reader.get("/report"), timeout=30)

    assert response.status_code == 422
    assert "rendering limits" in response.json()["detail"]
