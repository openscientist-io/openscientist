"""Graphviz diagrams in the final report: a ```dot block becomes an inline SVG
in the HTML and the PDF, cleaned of anything that reaches outside the diagram."""

from __future__ import annotations

import subprocess
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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


@pytest.fixture(autouse=True)
def fresh_diagram_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagrams, "_cache", OrderedDict())


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

    monkeypatch.setattr(subprocess, "run", run)
    assert diagrams.render_dot("digraph { " + "a -> b; " * diagrams.MAX_DOT_CHARS + "}") is None


def test_the_report_prompt_asks_for_dot_and_never_ascii() -> None:
    prompt = build_report_prompt(
        "Does X bind Y?", KnowledgeState("job", "Does X bind Y?", 3), context_window_tokens=131072
    )
    assert "```dot" in prompt
    assert "use ASCII diagrams" not in prompt
    assert "never as ASCII art" in prompt


@pytest.mark.requires_graphviz
def test_report_caps_graphviz_calls(tmp_path: Path) -> None:
    markdown = "\n".join(f"```dot\ndigraph {{ cap{i} -> b }}\n```" for i in range(12))
    with patch.object(subprocess, "run", wraps=subprocess.run) as run:
        html = _render(tmp_path, markdown)
        assert _render(tmp_path, markdown) == html
    assert run.call_count == 8
    assert html.count('<figure class="diagram">') == 8
    assert "cap11 -&gt; b" in html


def test_report_shares_a_wall_clock_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagrams, "REPORT_DIAGRAM_BUDGET_SECONDS", 0.2)
    run_process = subprocess.run

    def slow_dot(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return run_process(
            [sys.executable, "-c", "import time; time.sleep(0.15); print('<svg/>')"],
            timeout=float(str(kwargs["timeout"])),
            capture_output=True,
            text=True,
        )

    markdown = "\n".join(f"```dot\ndigraph {{ slow{i} -> b }}\n```" for i in range(4))
    with patch.object(subprocess, "run", side_effect=slow_dot) as run:
        started = time.monotonic()
        html = _render(tmp_path, markdown)
        elapsed = time.monotonic() - started
        calls = run.call_count
        started = time.monotonic()
        second = _render(tmp_path, markdown)
        assert time.monotonic() - started < 0.45
        assert run.call_count > calls
        assert second.count("<svg") > html.count("<svg")
    assert elapsed < 0.45
    assert "slow3 -&gt; b" in html


@pytest.mark.requires_graphviz
@pytest.mark.parametrize("source", ["digraph { cached -> b }", "digraph { cached -> }"])
def test_unchanged_report_reuses_diagram_results(tmp_path: Path, source: str) -> None:
    markdown = f"```dot\n{source}\n```"
    with patch.object(subprocess, "run", wraps=subprocess.run) as run:
        first = _render(tmp_path, markdown)
        second = _render(tmp_path, markdown)
    assert first == second
    assert run.call_count == 1


@pytest.mark.requires_graphviz
def test_changed_source_is_drawn(tmp_path: Path) -> None:
    with patch.object(subprocess, "run", wraps=subprocess.run) as run:
        first = _render(tmp_path, "```dot\ndigraph { original -> b }\n```")
        second = _render(tmp_path, "```dot\ndigraph { revised -> b }\n```")
    assert run.call_count == 2
    assert "original" in first
    assert "revised" in second


@pytest.mark.requires_graphviz
def test_diagram_cache_evicts_old_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagrams, "MAX_CACHED_DIAGRAMS", 2)
    with patch.object(subprocess, "run", wraps=subprocess.run) as run:
        for node in ("first", "second", "first", "third", "second"):
            assert diagrams.render_dot(f"digraph {{ {node} -> b }}") is not None
    assert run.call_count == 4


def test_busy_renderer_falls_back_without_waiting(tmp_path: Path) -> None:
    with diagrams._render_lock:
        with patch.object(subprocess, "run") as run:
            html = _render(tmp_path, "```dot\ndigraph { busy -> b }\n```")
    run.assert_not_called()
    assert "busy -&gt; b" in html


@pytest.mark.requires_graphviz
def test_cached_diagram_renders_while_another_thread_holds_the_slot(tmp_path: Path) -> None:
    markdown = "```dot\ndigraph { available -> b }\n```"
    first = _render(tmp_path, markdown)
    with patch.object(subprocess, "run") as run:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with diagrams._render_lock:
                second = pool.submit(_render, tmp_path, markdown).result(timeout=2)
    run.assert_not_called()
    assert "<svg" in second
    assert second == first


@pytest.mark.requires_graphviz
def test_expired_budget_does_not_cache_failure() -> None:
    source = "digraph { deferred -> b }"
    assert diagrams.render_dot(source, deadline=time.monotonic() - 1) is None
    assert diagrams.render_dot(source) is not None


def test_full_diagram_timeout_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagrams, "DOT_TIMEOUT_SECONDS", 0.1)
    with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("dot", 0.1)) as run:
        assert diagrams.render_dot("digraph { expensive -> b }") is None
        assert diagrams.render_dot("digraph { expensive -> b }") is None
    run.assert_called_once()
