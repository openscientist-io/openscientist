"""Markdown headings in an assistant chat bubble are sized to the bubble's
text, against NiceGUI's own markdown stylesheet, which sizes them for a page.

WeasyPrint resolves the cascade, so the test reads the font sizes a browser
would compute from the same two stylesheets.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import nicegui
import weasyprint  # type: ignore[import-untyped]

from openscientist.webapp_components.pages.job_detail.chat import _CHAT_STYLES

_NICEGUI_CSS = Path(nicegui.__file__).parent / "static" / "nicegui.css"
_BODY_PX = 16.0


def _boxes(box: Any) -> Iterator[Any]:
    yield box
    for child in getattr(box, "children", None) or []:
        yield from _boxes(child)


_HEADINGS = frozenset(f"h{level}" for level in range(1, 7))


def _font_sizes() -> dict[str, float]:
    """The computed font size of each heading level inside a bubble."""
    headings = "".join(f"<h{level}>Heading</h{level}>" for level in range(1, 7))
    html = (
        f"<html><head><style>{_NICEGUI_CSS.read_text(encoding='utf-8')}</style>"
        f"{_CHAT_STYLES}</head><body>"
        f"<div class='chat-bubble-assistant'><div class='nicegui-markdown'>{headings}"
        "</div></div></body></html>"
    )
    page = weasyprint.HTML(string=html).render().pages[0]
    return {
        box.element_tag: float(box.style["font_size"])
        for box in _boxes(page._page_box)
        if getattr(box, "element_tag", None) in _HEADINGS
    }


def test_bubble_headings_stay_close_to_the_bubble_text() -> None:
    sizes = _font_sizes()
    assert set(sizes) == {f"h{level}" for level in range(1, 7)}
    assert all(size <= _BODY_PX * 1.125 for size in sizes.values()), sizes


def test_bubble_headings_keep_their_order() -> None:
    sizes = _font_sizes()
    assert sizes["h1"] >= sizes["h2"] >= sizes["h3"] >= sizes["h4"]
