"""Font Awesome icons in the final report.

The agent writes an icon as raw HTML, ``<i class="fa-solid fa-flask"></i>``,
which the markdown conversion keeps. A report that uses one carries the
vendored stylesheets with the solid font inlined as a data URI, so the
sandboxed browser frame and WeasyPrint both draw it without fetching anything.
"""

from __future__ import annotations

import base64
import re
from functools import cache
from pathlib import Path

_FA_ASSETS = Path(__file__).resolve().parent.parent / "fa_assets"

#: The solid icons the report prompt offers, each with what it marks.
REPORT_ICONS: dict[str, str] = {
    "circle-check": "a supported hypothesis or a confirmed result",
    "circle-xmark": "a refuted hypothesis or a negative result",
    "circle-question": "an open or untested question",
    "triangle-exclamation": "a caveat or limitation",
    "lightbulb": "an insight or a new hypothesis",
    "scale-balanced": "mixed or conflicting evidence",
    "arrow-trend-up": "an increase",
    "arrow-trend-down": "a decrease",
    "arrow-right": "a next step",
    "flask": "an experiment",
    "vial": "a sample or an assay",
    "microscope": "imaging or observation",
    "dna": "genetics or sequencing",
    "virus": "a virus",
    "bacteria": "bacteria",
    "pills": "a drug or a treatment",
    "heart-pulse": "a clinical or physiological measure",
    "brain": "neuroscience",
    "chart-line": "a trend or a time course",
    "chart-column": "a comparison between groups",
    "table": "tabulated data",
    "database": "a dataset",
    "magnifying-glass": "a method or a search",
    "book-open": "the literature",
    "clock": "timing",
}

#: An icon element as the agent writes it, with the space after it.
_ICON_TAG = re.compile(
    r"""<i\b[^>]*\bclass\s*=\s*(["'])[^"']*\bfa-[a-z0-9-]+[^"']*\1[^>]*>\s*</i>[ \t]?"""
)
_USES_ICON = re.compile(r"""\bclass\s*=\s*["'][^"']*\bfa-solid\b""")


def uses_icons(html: str) -> bool:
    """Whether the report HTML contains a solid icon element."""
    return _USES_ICON.search(html) is not None


@cache
def icon_css() -> str:
    """The Font Awesome stylesheets with the solid font inlined."""
    font = base64.b64encode((_FA_ASSETS / "webfonts" / "fa-solid-900.woff2").read_bytes())
    solid = (_FA_ASSETS / "css" / "solid.min.css").read_text(encoding="utf-8")
    solid = solid.replace(
        "url(../webfonts/fa-solid-900.woff2)", f"url(data:font/woff2;base64,{font.decode()})"
    )
    core = (_FA_ASSETS / "css" / "fontawesome.min.css").read_text(encoding="utf-8")
    return f"{core}\n{solid}"


def strip_icon_tags(markdown: str) -> str:
    """The report without its icon elements, for a renderer that cannot draw them."""
    return _ICON_TAG.sub("", markdown)
