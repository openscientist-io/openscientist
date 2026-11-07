"""Font Awesome icons in the final report: the agent writes them as raw HTML,
the HTML and the WeasyPrint PDF draw them from the vendored font, and the
fpdf2 fallback drops them."""

from __future__ import annotations

import re
from pathlib import Path

import fitz  # type: ignore[import-untyped]  # PyMuPDF
import pytest

from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import build_report_prompt
from openscientist.pdf_generator import markdown_to_pdf
from openscientist.report.icons import REPORT_ICONS
from openscientist.report.renderer import render_report_html

_FA_CSS = Path(__file__).parent.parent / "src/openscientist/fa_assets/css/fontawesome.min.css"
_ICON_REPORT = (
    "# Report\n\n"
    '## <i class="fa-solid fa-flask"></i> Experiments\n\n'
    '- <i class="fa-solid fa-circle-check"></i> H1 is supported (p = 0.003)\n'
)


def _render(tmp_path: Path, markdown: str, *, embed: bool) -> str:
    md_path = tmp_path / "final_report.md"
    md_path.write_text(markdown, encoding="utf-8")
    return render_report_html(md_path, tmp_path, embed_images=embed)


def _pdf_fonts(pdf_path: Path) -> set[str]:
    """The base names of the fonts embedded in the PDF, without separators."""
    with fitz.open(pdf_path) as doc:
        return {
            font[3].replace("-", "").replace(" ", "") for page in doc for font in page.get_fonts()
        }


@pytest.mark.parametrize("name", sorted(REPORT_ICONS))
def test_every_offered_icon_exists_in_the_vendored_font(name: str) -> None:
    assert re.search(rf"\.fa-{re.escape(name)}[,{{]", _FA_CSS.read_text(encoding="utf-8"))


@pytest.mark.parametrize("embed", [True, False], ids=["browser", "pdf"])
def test_a_report_with_icons_carries_the_font_inline(tmp_path: Path, embed: bool) -> None:
    """The font travels inside the report, because the sandboxed browser frame
    fetches nothing from the app and WeasyPrint reads only what it is given."""
    html = _render(tmp_path, _ICON_REPORT, embed=embed)
    assert '<i class="fa-solid fa-flask"></i>' in html
    assert ".fa-flask" in html
    assert "src:url(data:font/woff2;base64," in html
    assert "../webfonts/" not in html


def test_a_report_without_icons_carries_no_icon_font(tmp_path: Path) -> None:
    html = _render(tmp_path, "# Report\n\nPlain text.\n", embed=True)
    assert "Font Awesome" not in html
    assert "data:font/woff2" not in html


@pytest.mark.requires_weasyprint
def test_the_weasyprint_pdf_draws_the_icons_from_the_font(tmp_path: Path) -> None:
    from weasyprint import HTML  # type: ignore[import-untyped]

    with_icons = tmp_path / "icons.pdf"
    HTML(string=_render(tmp_path, _ICON_REPORT, embed=False)).write_pdf(with_icons)
    without_icons = tmp_path / "plain.pdf"
    HTML(string=_render(tmp_path, "# Report\n\nPlain text.\n", embed=False)).write_pdf(
        without_icons
    )
    assert any("FontAwesome" in font for font in _pdf_fonts(with_icons))
    assert not any("FontAwesome" in font for font in _pdf_fonts(without_icons))


def test_the_fallback_pdf_drops_the_icon_markup(tmp_path: Path) -> None:
    md_path = tmp_path / "final_report.md"
    md_path.write_text(_ICON_REPORT, encoding="utf-8")
    pdf_path = markdown_to_pdf(md_path, tmp_path / "fallback.pdf", add_footer=False)
    with fitz.open(pdf_path) as doc:
        text = "\n".join(page.get_text() for page in doc)
    assert "Experiments" in text
    assert "H1 is supported" in text
    assert "<i" not in text
    assert "fa-solid" not in text


def test_the_report_prompt_offers_the_icons() -> None:
    prompt = build_report_prompt(
        "Does X bind Y?", KnowledgeState("job", "Does X bind Y?", 3), context_window_tokens=131072
    )
    assert '<i class="fa-solid fa-circle-check"></i>' in prompt
    for name in REPORT_ICONS:
        assert f"`fa-{name}`" in prompt
