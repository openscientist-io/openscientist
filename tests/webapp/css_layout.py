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

NICEGUI_STATIC = Path(nicegui.__file__).parent / "static"
NICEGUI_CSS = NICEGUI_STATIC / "nicegui.css"
_QUASAR_UNIMPORTANT = NICEGUI_STATIC / "quasar.unimportant.prod.css"
_QUASAR_IMPORTANT = NICEGUI_STATIC / "quasar.important.prod.css"


def _boxes(box: Any) -> Iterator[Any]:
    yield box
    for child in getattr(box, "children", None) or []:
        yield from _boxes(child)


#: The cascade-layer order NiceGUI's page template declares for the browser.
_LAYER_ORDER = (
    "@layer theme, base, quasar, nicegui, components, utilities, overrides, quasar_importants;"
)


def layout(
    body: str, *styles: str, quasar: bool = False, page_width: int | None = None
) -> list[Any]:
    """Every box of ``body`` laid out under NiceGUI's CSS and ``styles``,
    each a ``<style> block or bare CSS. With ``quasar``, Quasar's own
    stylesheets join under the browser's layer order, where the unlayered app
    styles still win on the properties they declare. ``page_width`` fixes the
    page width in px, for column layout at a chosen card width."""
    page_size = (
        f"<style>@page {{ size: {page_width}px 4000px; margin: 0; }}</style>"
        if page_width is not None
        else ""
    )
    if quasar:
        head = (
            "<style>"
            + _LAYER_ORDER
            + f"@layer quasar {_QUASAR_UNIMPORTANT.read_text(encoding='utf-8')}"
            + f"@layer quasar_importants {_QUASAR_IMPORTANT.read_text(encoding='utf-8')}"
            + f"@layer nicegui {NICEGUI_CSS.read_text(encoding='utf-8')}"
            + "</style>"
        )
    else:
        head = f"<style>{NICEGUI_CSS.read_text(encoding='utf-8')}</style>"
    head += "".join(s if s.lstrip().startswith("<style") else f"<style>{s}</style>" for s in styles)
    html = f"<html><head>{page_size}{head}</head><body>{body}</body></html>"
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
