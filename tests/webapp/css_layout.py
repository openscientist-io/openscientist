"""Lay out a page fragment against NiceGUI's own stylesheet and the app's.

WeasyPrint resolves the cascade and the flex layout the way a browser does
for these rules, so a test can read the font sizes and vertical offsets the
browser would compute. Tailwind's utilities are generated in the browser and
take no part. WeasyPrint loads Pango when imported, so the import waits for
a layout and a test using one carries the ``requires_weasyprint`` marker.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import nicegui

NICEGUI_CSS = Path(nicegui.__file__).parent / "static" / "nicegui.css"


def _boxes(box: Any) -> Iterator[Any]:
    yield box
    for child in getattr(box, "children", None) or []:
        yield from _boxes(child)


def layout(body: str, *styles: str) -> list[Any]:
    """Every box of ``body`` laid out under NiceGUI's CSS and ``styles``,
    each a ``<style>`` block or bare CSS."""
    sheets = "".join(
        s if s.lstrip().startswith("<style") else f"<style>{s}</style>" for s in styles
    )
    html = (
        f"<html><head><style>{NICEGUI_CSS.read_text(encoding='utf-8')}</style>{sheets}</head>"
        f"<body>{body}</body></html>"
    )
    import weasyprint  # type: ignore[import-untyped]

    page = weasyprint.HTML(string=html).render().pages[0]
    return list(_boxes(page._page_box))


def box_by_id(boxes: list[Any], element_id: str) -> Any:
    """The first box an element with ``element_id`` generated."""
    for box in boxes:
        element = getattr(box, "element", None)
        if element is not None and element.get("id") == element_id:
            return box
    raise AssertionError(f"no box for #{element_id}")
