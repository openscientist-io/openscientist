"""Markdown headings in an assistant chat bubble are sized to the bubble's
text, against NiceGUI's own markdown stylesheet, which sizes them for a page."""

from __future__ import annotations

from openscientist.webapp_components.pages.job_detail.chat import _CHAT_STYLES
from tests.webapp.css_layout import layout

_BODY_PX = 16.0
_HEADINGS = frozenset(f"h{level}" for level in range(1, 7))


def _font_sizes() -> dict[str, float]:
    """The computed font size of each heading level inside a bubble."""
    headings = "".join(f"<h{level}>Heading</h{level}>" for level in range(1, 7))
    boxes = layout(
        f"<div class='chat-bubble-assistant'><div class='nicegui-markdown'>{headings}</div></div>",
        _CHAT_STYLES,
    )
    return {
        box.element_tag: float(box.style["font_size"])
        for box in boxes
        if getattr(box, "element_tag", None) in _HEADINGS
    }


def test_bubble_headings_stay_close_to_the_bubble_text() -> None:
    sizes = _font_sizes()
    assert set(sizes) == _HEADINGS
    assert all(size <= _BODY_PX * 1.125 for size in sizes.values()), sizes


def test_bubble_headings_keep_their_order() -> None:
    sizes = _font_sizes()
    assert sizes["h1"] >= sizes["h2"] >= sizes["h3"] >= sizes["h4"]
