"""Tests for figure-tag isolation and HTML rendering.

Regression coverage for the inline-figure bug: the report model appends
``{{figure:...}}`` tags to the end of bullet/text lines, but the block-level
``FigureBlockProcessor`` only converts tags alone on their own line, so figures
never embedded. ``isolate_figure_tags`` puts them on their own line first.
"""

from __future__ import annotations

from pathlib import Path

from openscientist.report.processor import isolate_figure_tags
from openscientist.report.renderer import render_report_html

# A minimal valid 1x1 PNG so the renderer can read/base64 it.
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6200010000050001"
    "0d0a2db40000000049454e44ae426082"
)


# --- isolate_figure_tags (pure string in/out) ---------------------------------


def test_inline_tag_on_bullet_is_split_onto_its_own_line() -> None:
    src = "* **Palmitic acid** - p = 0.008.{{figure:fa.png|caption=Boxplot}}"
    out = isolate_figure_tags(src)
    lines = [ln for ln in out.split("\n") if ln.strip()]
    # The tag now stands alone (no leading text on its line).
    assert "{{figure:fa.png|caption=Boxplot}}" in lines
    assert any(ln.strip().startswith("* **Palmitic acid**") for ln in lines)


def test_multiple_tags_on_one_line_each_isolated() -> None:
    src = "text {{figure:a.png|caption=A}} more {{figure:b.png|caption=B}} end"
    out = isolate_figure_tags(src)
    standalone = [ln.strip() for ln in out.split("\n") if ln.strip()]
    assert "{{figure:a.png|caption=A}}" in standalone
    assert "{{figure:b.png|caption=B}}" in standalone


def test_tag_inside_fenced_code_block_is_left_raw() -> None:
    src = "```\nshow {{figure:x.png|caption=C}} here\n```"
    out = isolate_figure_tags(src)
    # The fenced content is unchanged (the tag was not split out).
    assert "show {{figure:x.png|caption=C}} here" in out


def test_genuine_table_row_is_left_intact() -> None:
    src = "| Metabolite | p |\n| FA 16:0 | 0.008 |"
    assert isolate_figure_tags(src) == src


def test_parameterized_tag_is_not_mistaken_for_a_table_row() -> None:
    # The tag's own '|' separator must not cause the line to be skipped.
    src = "Finding here.{{figure:fa.png|caption=Boxplot|width=80%}}"
    out = isolate_figure_tags(src)
    assert any(
        ln.strip() == "{{figure:fa.png|caption=Boxplot|width=80%}}" for ln in out.split("\n")
    )


# --- end-to-end rendering -----------------------------------------------------


def _job_dir(tmp_path: Path, *figures: str) -> Path:
    prov = tmp_path / "provenance"
    prov.mkdir()
    for name in figures:
        (prov / name).write_bytes(_PNG_1x1)
    return tmp_path


def _render(tmp_path: Path, markdown: str, *, embed: bool = False) -> str:
    md_path = tmp_path / "final_report.md"
    md_path.write_text(markdown, encoding="utf-8")
    return render_report_html(md_path, tmp_path, embed_images=embed)


def test_inline_figure_embeds_in_html(tmp_path: Path) -> None:
    job_dir = _job_dir(tmp_path, "fa.png")
    md = "## Findings\n\n* **Palmitic acid** - p = 0.008.{{figure:fa.png|caption=Boxplot of FA}}\n"
    html = _render(job_dir, md)
    assert "<img" in html
    assert "<figcaption" in html
    assert "Boxplot of FA" in html
    assert "{{figure" not in html  # the raw tag must be gone


def test_inline_figure_embeds_base64_when_requested(tmp_path: Path) -> None:
    job_dir = _job_dir(tmp_path, "fa.png")
    md = "Text.{{figure:fa.png|caption=Cap}}"
    html = _render(job_dir, md, embed=True)
    assert html.count("data:image") == 1


def test_standalone_tag_not_double_rendered(tmp_path: Path) -> None:
    job_dir = _job_dir(tmp_path, "fa.png")
    md = "## Section\n\n{{figure:fa.png|caption=Standalone}}\n\nMore text.\n"
    html = _render(job_dir, md)
    assert html.count("<img") == 1


def test_two_inline_tags_produce_two_images(tmp_path: Path) -> None:
    job_dir = _job_dir(tmp_path, "a.png", "b.png")
    md = "* A.{{figure:a.png|caption=A}}\n* B.{{figure:b.png|caption=B}}\n"
    html = _render(job_dir, md)
    assert html.count("<img") == 2


def test_missing_figure_falls_back_to_caption_text(tmp_path: Path) -> None:
    job_dir = _job_dir(tmp_path)  # no figure files
    md = "Finding.{{figure:nope.png|caption=Missing figure}}"
    html = _render(job_dir, md)
    assert "[Figure: Missing figure]" in html
    assert "<img" not in html
    assert "{{figure" not in html
