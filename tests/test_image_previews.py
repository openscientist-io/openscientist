"""A job keeps a small viewing copy of each attached image beside its untouched original."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncEngine

from openscientist.image_previews import MAX_EDGE_PX, PREVIEW_DIR, preview_name, write_previews
from tests.test_job_dir_permissions import JOB_DIR_MODE, JOB_FILE_MODE, ChownRecorder, _mode


def _photo(path: Path, size: tuple[int, int], mode: str = "RGB") -> Path:
    channels = {"RGB": 3, "RGBA": 4}[mode]
    pixels = np.random.default_rng(3).integers(0, 256, (size[1], size[0], channels), dtype=np.uint8)
    Image.fromarray(pixels, mode).save(path)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_large_photo_gets_a_copy_at_the_size_the_model_sees(tmp_path: Path) -> None:
    original = _photo(tmp_path / "gel.png", (4000, 2500), "RGBA")
    before = _digest(original)

    [preview] = write_previews([original], tmp_path / "job")

    assert preview == tmp_path / "job" / PREVIEW_DIR / "gel.png.jpg"
    with Image.open(preview) as image:
        assert image.format == "JPEG"
        assert max(image.size) == MAX_EDGE_PX
        assert image.size[0] / image.size[1] == pytest.approx(4000 / 2500, rel=0.01)
    assert preview.stat().st_size < original.stat().st_size
    assert _digest(original) == before


def test_a_small_photo_keeps_its_size(tmp_path: Path) -> None:
    original = _photo(tmp_path / "blot.jpg", (640, 480))
    [preview] = write_previews([original], tmp_path / "job")
    with Image.open(preview) as image:
        assert image.size == (640, 480)


def test_two_images_sharing_a_stem_get_separate_copies(tmp_path: Path) -> None:
    png = _photo(tmp_path / "fig.png", (300, 200))
    jpg = _photo(tmp_path / "fig.jpg", (300, 200))
    previews = write_previews([png, jpg], tmp_path / "job")
    assert [p.name for p in previews] == [preview_name("fig.png"), preview_name("fig.jpg")]


def test_other_files_and_unreadable_images_get_no_copy(tmp_path: Path) -> None:
    table = tmp_path / "counts.csv"
    table.write_text("a,b\n1,2\n", encoding="utf-8")
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png")
    assert write_previews([table, broken], tmp_path / "job") == []
    assert broken.read_bytes().endswith(b"not really a png")


def test_a_new_job_has_agent_owned_copies_of_its_images(
    tmp_path: Path, test_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openscientist.orchestrator.setup import create_job

    chowns = ChownRecorder(monkeypatch)
    photo = _photo(tmp_path / "gel.jpg", (2400, 1600))
    table = tmp_path / "counts.csv"
    table.write_text("a,b\n1,2\n", encoding="utf-8")

    job_dir = create_job(str(uuid4()), "Question?", [photo, table], 5, jobs_dir=tmp_path / "jobs")

    previews = job_dir / PREVIEW_DIR
    assert [p.name for p in previews.iterdir()] == ["gel.jpg.jpg"]
    assert _digest(job_dir / "data" / "gel.jpg") == _digest(photo)
    assert previews.stat().st_mode & stat.S_ISGID
    assert _mode(previews) == JOB_DIR_MODE
    assert _mode(previews / "gel.jpg.jpg") == JOB_FILE_MODE
    assert chowns.in_jobs(previews)
    assert chowns.in_jobs(previews / "gel.jpg.jpg")
