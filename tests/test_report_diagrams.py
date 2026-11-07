"""Graphviz diagrams in the final report: a ```dot block becomes an inline SVG
in the HTML and the PDF, cleaned of anything that reaches outside the diagram."""

from __future__ import annotations

from pathlib import Path

import fitz  # type: ignore[import-untyped]
import pytest

from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import build_report_prompt
from openscientist.report import diagrams
from openscientist.report.renderer import render_report_html

_MODEL = """\
## Mechanistic model

```dot
digraph model {
  "Chronic energy deficit" -> "Marrow hypoplasia";
  "Chronic energy deficit" -> "Micronutrient depletion";
  "Marrow hypoplasia" -> "WBC 3.9, ALC 0.6";
}
```

The deficit explains both branches.
"""


def _render(tmp_path: Path, markdown: str, *, embed: bool = True) -> str:
    (tmp_path / "final_report.md").write_text(markdown, encoding="utf-8")
    return render_report_html(tmp_path / "final_report.md", tmp_path, embed_images=embed)


@pytest.mark.requires_graphviz
@pytest.mark.parametrize("embed", [True, False], ids=["browser", "pdf"])
def test_a_dot_block_becomes_an_inline_svg_diagram(tmp_path: Path, embed: bool) -> None:
    html = _render(tmp_path, _MODEL, embed=embed)
    assert '<figure class="diagram">' in html
    assert "<svg" in html
    assert "Marrow hypoplasia" in html
    assert "digraph model" not in html
    assert "The deficit explains both branches." in html


@pytest.mark.requires_graphviz
@pytest.mark.requires_weasyprint
def test_the_pdf_draws_the_diagram_labels(tmp_path: Path) -> None:
    from weasyprint import HTML  # type: ignore[import-untyped]

    pdf = tmp_path / "report.pdf"
    HTML(string=_render(tmp_path, _MODEL, embed=False)).write_pdf(pdf)
    with fitz.open(pdf) as doc:
        text = " ".join(page.get_text() for page in doc)
    assert "Marrow hypoplasia" in text
    assert "Micronutrient depletion" in text


def test_a_dot_block_inside_another_fence_stays_code(tmp_path: Path) -> None:
    markdown = "````markdown\n```dot\ndigraph { a -> b }\n```\n````\n"
    html = _render(tmp_path, markdown)
    assert "<svg" not in html
    assert "digraph { a -&gt; b }" in html


@pytest.mark.requires_graphviz
@pytest.mark.parametrize(
    ("source", "leak"),
    [
        ('digraph { a [image="/etc/hostname"] }', "/etc/hostname"),
        ('digraph { a [URL="javascript:alert(1)"] }', "javascript:"),
        ('digraph { a [href="https://tracker.test/x"] }', "tracker.test"),
        ('digraph { stylesheet="file:///etc/passwd"; a }', "/etc/passwd"),
    ],
)
def test_a_diagram_reaches_nothing_outside_itself(source: str, leak: str) -> None:
    svg = diagrams.render_dot(source)
    assert svg is not None
    assert leak not in svg
    assert "<image" not in svg
    assert "<a " not in svg


@pytest.mark.requires_graphviz
def test_an_invalid_diagram_shows_its_source(tmp_path: Path) -> None:
    html = _render(tmp_path, "```dot\ndigraph { a -> }\n```\n")
    assert "<svg" not in html
    assert "The diagram could not be drawn." in html
    assert "digraph { a -&gt; }" in html


def test_an_oversized_diagram_is_not_drawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(*args: object, **kwargs: object) -> object:
        raise AssertionError("dot ran on an oversized diagram")

    monkeypatch.setattr(diagrams.subprocess, "run", run)
    assert diagrams.render_dot("digraph { " + "a -> b; " * diagrams.MAX_DOT_CHARS + "}") is None


def test_the_report_prompt_asks_for_dot_and_never_ascii() -> None:
    prompt = build_report_prompt(
        "Does X bind Y?", KnowledgeState("job", "Does X bind Y?", 3), context_window_tokens=131072
    )
    assert "```dot" in prompt
    assert "use ASCII diagrams" not in prompt
    assert "never as ASCII art" in prompt
