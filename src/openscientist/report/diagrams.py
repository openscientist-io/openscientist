"""Graphviz diagrams in the final report.

A fenced ```dot block in the report's markdown becomes an inline SVG drawn by
Graphviz, which the browser and WeasyPrint both render. The DOT source is
agent-written, so Graphviz runs with its image file access closed, under a
timeout and a size cap, and the SVG keeps only drawing elements, without
links, images or stylesheets that could reach outside the diagram.
"""

from __future__ import annotations

import html
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from markdown import Markdown  # type: ignore[import-untyped]
from markdown.extensions import Extension  # type: ignore[import-untyped]
from markdown.preprocessors import Preprocessor  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

MAX_DOT_CHARS = 20_000
MAX_SVG_CHARS = 2_000_000
DOT_TIMEOUT_SECONDS = 10

_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_DRAWING_TAGS = frozenset(
    {
        "svg",
        "g",
        "title",
        "path",
        "polygon",
        "polyline",
        "ellipse",
        "circle",
        "rect",
        "line",
        "text",
        "tspan",
        "defs",
        "linearGradient",
        "radialGradient",
        "stop",
    }
)
_LOCAL_REFERENCE = re.compile(r"^url\(#[\w.-]+\)$")
#: Defaults in the report's palette, which a diagram's own attributes override.
_DEFAULT_STYLE = (
    "-Gfontname=Helvetica",
    "-Gnodesep=0.3",
    "-Granksep=0.45",
    "-Nfontname=Helvetica",
    "-Nfontsize=12",
    "-Nshape=box",
    "-Nstyle=rounded,filled",
    "-Nfillcolor=#f4f6f9",
    "-Ncolor=#34495e",
    "-Nfontcolor=#2c3e50",
    "-Nmargin=0.15,0.08",
    "-Efontname=Helvetica",
    "-Efontsize=10",
    "-Ecolor=#5d6d7e",
)

# An opening fence and the info string that marks a Graphviz block.
_FENCE_OPEN = re.compile(r"^(?P<fence>`{3,}|~{3,})\s*(?P<info>[\w+-]*)")


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _clean(element: ET.Element) -> None:
    """Keep the drawing elements and attributes, in place. A link keeps its
    drawing and loses itself, and anything else goes with its content."""
    children: list[ET.Element] = []
    for child in list(element):
        tag = _local(child.tag)
        if tag == "a":
            _clean(child)
            children.extend(child)
        elif tag in _DRAWING_TAGS:
            _clean(child)
            children.append(child)
    for child in list(element):
        element.remove(child)
    element.extend(children)
    for name, value in list(element.attrib.items()):
        local = _local(name)
        if (
            local == "href"
            or local == "style"
            or local.startswith("on")
            or ("url(" in value and not _LOCAL_REFERENCE.match(value.strip()))
        ):
            del element.attrib[name]


def render_dot(source: str) -> str | None:
    """The diagram as a cleaned SVG fragment, or None when it cannot be drawn."""
    if len(source) > MAX_DOT_CHARS:
        logger.warning("Diagram of %d characters is over the %d cap", len(source), MAX_DOT_CHARS)
        return None
    # SERVER_NAME makes Graphviz refuse image files outside GV_FILE_PATH, which
    # points nowhere, so an image attribute cannot pull a local file in.
    env = {"PATH": os.environ.get("PATH", ""), "SERVER_NAME": "openscientist"}
    env["GV_FILE_PATH"] = "/nonexistent"
    try:
        with tempfile.TemporaryDirectory() as scratch:
            result = subprocess.run(
                ("dot", "-Tsvg", *_DEFAULT_STYLE),
                input=source,
                capture_output=True,
                text=True,
                timeout=DOT_TIMEOUT_SECONDS,
                env=env,
                cwd=scratch,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Graphviz did not draw a diagram: %s", exc)
        return None
    if result.returncode != 0 or not result.stdout or len(result.stdout) > MAX_SVG_CHARS:
        logger.warning("Graphviz refused a diagram: %s", result.stderr.strip()[:300])
        return None
    try:
        root = ET.fromstring(result.stdout)
    except ET.ParseError as exc:
        logger.warning("Graphviz wrote an unreadable SVG: %s", exc)
        return None
    _clean(root)
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", _XLINK_NS)
    return ET.tostring(root, encoding="unicode")


def _diagram_html(source: str) -> str:
    svg = render_dot(source)
    if svg is not None:
        return f'<figure class="diagram">{svg}</figure>'
    return (
        '<figure class="diagram diagram-failed"><p>The diagram could not be drawn.</p>'
        f"<pre><code>{html.escape(source.strip())}</code></pre></figure>"
    )


class _DiagramPreprocessor(Preprocessor):  # type: ignore[misc]
    """Swap each top-level ```dot block for its drawn diagram, before the
    fenced code extension sees it. A dot block inside another fence is code."""

    def run(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        index = 0
        while index < len(lines):
            match = _FENCE_OPEN.match(lines[index])
            if match is None:
                out.append(lines[index])
                index += 1
                continue
            fence = match["fence"]
            close = re.compile(rf"^{re.escape(fence[0])}{{{len(fence)},}}\s*$")
            end = next(
                (i for i in range(index + 1, len(lines)) if close.match(lines[i])), len(lines)
            )
            block = lines[index : end + 1]
            if match["info"].lower() in ("dot", "graphviz") and end < len(lines):
                placeholder = self.md.htmlStash.store(_diagram_html("\n".join(block[1:-1])))
                out.extend(["", placeholder, ""])
            else:
                out.extend(block)
            index = end + 1
        return out


class DiagramExtension(Extension):  # type: ignore[misc]
    def extendMarkdown(self, md: Markdown) -> None:  # noqa: N802
        # Ahead of fenced_code (25), which would turn the block into code.
        md.preprocessors.register(_DiagramPreprocessor(md), "graphviz_diagram", 30)
