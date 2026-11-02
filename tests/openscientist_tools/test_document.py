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
