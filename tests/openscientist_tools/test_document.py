"""End-to-end stdio tests for the standalone `read_document` tool."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pymupdf  # type: ignore[import-untyped]
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent


def _make_pdf(path: Path, body: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), body, fontsize=11)
    doc.save(path)
    doc.close()


async def _call_read_document(params: StdioServerParameters, file_path: str) -> str:
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("read_document", {"file_path": file_path})
            (block,) = result.content
            assert isinstance(block, TextContent)
            return block.text


async def test_reads_absolute_path(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    pdf = tmp_path / "sample.pdf"
    _make_pdf(pdf, "hello standalone reader")
    text = await _call_read_document(server_params(server_env(tmp_path)), str(pdf))
    assert text.startswith("[PDF: ")
    assert "hello standalone reader" in text


async def test_reads_relative_path_from_job_data_dir(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    (tmp_path / "data").mkdir()
    _make_pdf(tmp_path / "data" / "sample.pdf", "content via relative path")
    text = await _call_read_document(server_params(server_env(tmp_path)), "sample.pdf")
    assert text.startswith("[PDF: ")
    assert "content via relative path" in text


async def test_relative_path_strips_nested_components(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    (tmp_path / "data").mkdir()
    _make_pdf(tmp_path / "data" / "sample.pdf", "basename-only resolution")
    text = await _call_read_document(
        server_params(server_env(tmp_path)), "ignored_subdir/sample.pdf"
    )
    assert "basename-only resolution" in text


async def test_missing_file_lists_available_files(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "other.txt").write_text("present")
    text = await _call_read_document(server_params(server_env(tmp_path)), "missing.pdf")
    assert text.startswith("❌ File not found: missing.pdf")
    assert "  - other.txt" in text


async def test_missing_file_with_no_data_dir(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    text = await _call_read_document(server_params(server_env(tmp_path)), "missing.pdf")
    # The error names the missing file
    assert text.startswith("❌ File not found")
    assert "missing.pdf" in text


async def test_absolute_path_outside_job_dir_refused(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "data").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-42")
    text = await _call_read_document(server_params(server_env(job_dir)), str(secret))
    assert "TOPSECRET-42" not in text
    assert text.startswith("❌")


async def test_traversal_path_outside_job_dir_refused(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "data").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-43")
    text = await _call_read_document(
        server_params(server_env(job_dir)), f"{job_dir}/data/../../secret.txt"
    )
    assert "TOPSECRET-43" not in text
    assert text.startswith("❌")


async def test_symlink_pointing_out_of_job_dir_refused(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "data").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-44")
    (job_dir / "data" / "link.txt").symlink_to(secret)
    text = await _call_read_document(server_params(server_env(job_dir)), "link.txt")
    assert "TOPSECRET-44" not in text
    assert text.startswith("❌")


async def test_absolute_path_inside_job_dir_read(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "data").mkdir(parents=True)
    note = job_dir / "data" / "note.txt"
    note.write_text("insider note 77")
    text = await _call_read_document(server_params(server_env(job_dir)), str(note))
    assert "insider note 77" in text
