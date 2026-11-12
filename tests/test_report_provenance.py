"""A report written at the end of a run names the environment the run recorded."""

from __future__ import annotations

from pathlib import Path

import pymupdf  # type: ignore[import-untyped]
import pytest
from PIL import Image
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist.database.models import Job
from openscientist.orchestrator.report import _try_generate_report_pdf

_RECORDED = {
    "openscientist_commit": "abc123def456",
    "agent_harness": "claude_code",
    "agent_harness_version": "2.1.3",
}


@pytest.mark.asyncio
async def test_a_generated_report_names_the_run_environment(
    test_engine: AsyncEngine, tmp_path: Path
) -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        job = Job(research_question="Q?", status="generating_report", version_info=_RECORDED)
        session.add(job)
        await session.commit()
    try:
        job_dir = tmp_path / str(job.id)
        job_dir.mkdir()
        report_path = job_dir / "final_report.md"
        report_path.write_text("# Report\n\nFindings.\n", encoding="utf-8")

        await _try_generate_report_pdf(report_path)

        html = (job_dir / "final_report.html").read_text(encoding="utf-8")
        assert "OpenScientist abc123def456" in html
        assert "Claude Code 2.1.3" in html
    finally:
        async with AsyncSession(test_engine) as session:
            await session.execute(delete(Job).where(Job.id == job.id))
            await session.commit()


@pytest.mark.asyncio
async def test_a_report_for_an_unknown_job_still_renders(tmp_path: Path) -> None:
    """The footer is best effort, so a missing record never costs the report."""
    job_dir = tmp_path / "not-a-job"
    job_dir.mkdir()
    report_path = job_dir / "final_report.md"
    report_path.write_text("# Report\n", encoding="utf-8")
    await _try_generate_report_pdf(report_path)
    assert "Run environment" not in (job_dir / "final_report.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("input_dir", ["data", "previews"])
async def test_persisted_reports_withhold_uploaded_images(tmp_path: Path, input_dir: str) -> None:
    job_dir = tmp_path / "not-a-job"
    (job_dir / input_dir).mkdir(parents=True)
    (job_dir / "provenance").mkdir()
    private_image = job_dir / input_dir / "upload.png"
    public_image = job_dir / "provenance" / "figure.png"
    Image.new("RGB", (13, 17), "red").save(private_image)
    Image.new("RGB", (19, 23), "blue").save(public_image)
    report_path = job_dir / "final_report.md"
    report_path.write_text(
        f"# Report\n\n![Input]({input_dir}/upload.png)\n\n"
        "{{figure:figure.png|caption=Generated figure}}\n",
        encoding="utf-8",
    )

    await _try_generate_report_pdf(report_path)

    html = (job_dir / "final_report.html").read_text(encoding="utf-8")
    assert private_image.as_uri() not in html
    assert f'src="{input_dir}/upload.png"' not in html
    assert "Input" in html
    assert public_image.as_uri() in html
    with pymupdf.open(job_dir / "final_report.pdf") as document:
        image_sizes = {(image[2], image[3]) for page in document for image in page.get_images()}
    assert (13, 17) not in image_sizes
    assert (19, 23) in image_sizes


def test_public_pdf_fetches_only_embedded_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.request import OpenerDirector, Request

    from openscientist.report import pdf

    (tmp_path / "data").mkdir()
    (tmp_path / "previews").mkdir()
    (tmp_path / "provenance").mkdir()
    private_image = tmp_path / "data" / "upload.png"
    Image.new("RGB", (13, 17), "red").save(private_image)
    Image.new("RGB", (11, 15), "green").save(tmp_path / "previews" / "upload.png.jpg")
    Image.new("RGB", (19, 23), "blue").save(tmp_path / "provenance" / "figure.png")
    (tmp_path / "final_report.md").write_text(
        "# Report\n\n![Input](data/upload.png)\n\n![Preview](previews/upload.png.jpg)\n\n"
        "{{figure:figure.png|caption=Generated figure}}\n\n"
        f'<img src="{private_image.as_uri()}">\n\n'
        '<img src="https://private.invalid/image.png">\n\n'
        '<style>@import url("https://private.invalid/style.css");</style>',
        encoding="utf-8",
    )
    fetched: list[str] = []
    real_open = OpenerDirector.open

    def recording_open(
        opener: OpenerDirector,
        request: Request | str,
        data: bytes | None = None,
        timeout: float = 10,
    ) -> object:
        url = request if isinstance(request, str) else request.full_url
        fetched.append(url)
        if not url.startswith("data:"):
            raise ValueError("Unexpected external resource")
        return real_open(opener, request, data, timeout)

    monkeypatch.setattr(OpenerDirector, "open", recording_open)
    rendered = pdf.render_public_report_pdf(tmp_path)

    assert fetched
    assert all(url.startswith("data:") for url in fetched)
    with pymupdf.open(stream=rendered, filetype="pdf") as document:
        image_sizes = {(image[2], image[3]) for page in document for image in page.get_images()}
    assert image_sizes == {(19, 23)}
    assert not (tmp_path / "final_report.pdf").exists()
