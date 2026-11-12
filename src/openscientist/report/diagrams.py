"""Graphviz diagrams in the final report.

A fenced ```dot block in the report's markdown becomes an inline SVG drawn by
Graphviz, which the browser and WeasyPrint both render. The DOT source is
agent-written, so Graphviz runs with its image file access closed, under a
timeout and a size cap, and the SVG keeps only drawing elements, without
links, images or stylesheets that could reach outside the diagram.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from threading import Lock
from time import monotonic

from markdown import Markdown  # type: ignore[import-untyped]
from markdown.extensions import Extension  # type: ignore[import-untyped]
from markdown.preprocessors import Preprocessor  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

MAX_DOT_CHARS = 20_000
MAX_SVG_CHARS = 2_000_000
DOT_TIMEOUT_SECONDS = 10
MAX_REPORT_DIAGRAMS = 8
REPORT_DIAGRAM_BUDGET_SECONDS = 2.0
MAX_CACHED_DIAGRAMS = 16

_cache: OrderedDict[bytes, str | None] = OrderedDict()
_cache_lock = Lock()
_render_lock = Lock()

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


def render_dot(source: str, *, deadline: float | None = None) -> str | None:
    """The diagram as a cleaned SVG fragment, or None when it cannot be drawn."""
    if len(source) > MAX_DOT_CHARS:
        logger.warning("Diagram of %d characters is over the %d cap", len(source), MAX_DOT_CHARS)
        return None
    key = hashlib.sha256(source.encode("utf-8")).digest()
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    # Uncached diagrams fall back to source instead of queuing behind Graphviz.
    if not _render_lock.acquire(blocking=False):
        return None
    try:
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)
                return _cache[key]
        remaining = (
            DOT_TIMEOUT_SECONDS
            if deadline is None
            else min(DOT_TIMEOUT_SECONDS, deadline - monotonic())
        )
        if remaining <= 0:
            return None
        try:
            svg = _render_dot(source, remaining)
        except subprocess.TimeoutExpired as exc:
            logger.warning("Graphviz did not draw a diagram: %s", exc)
            if remaining < DOT_TIMEOUT_SECONDS:
                return None
            svg = None
        except OSError as exc:
            logger.warning("Graphviz did not draw a diagram: %s", exc)
            return None
        with _cache_lock:
            _cache[key] = svg
            if len(_cache) > MAX_CACHED_DIAGRAMS:
                _cache.popitem(last=False)
        return svg
    finally:
        _render_lock.release()


def _render_dot(source: str, timeout: float) -> str | None:
    # SERVER_NAME makes Graphviz refuse to load any file, so an image attribute
    # cannot pull a local file in. Graphviz 14 exits when GV_FILE_PATH is set.
    env = {"PATH": os.environ.get("PATH", ""), "SERVER_NAME": "openscientist"}
    with tempfile.TemporaryDirectory() as scratch:
        result = subprocess.run(
            ("dot", "-Tsvg", *_DEFAULT_STYLE),
            input=source,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=scratch,
        )
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


def _diagram_html(source: str, *, deadline: float, allowed: bool) -> str:
    svg = render_dot(source, deadline=deadline) if allowed else None
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
        deadline = monotonic() + REPORT_DIAGRAM_BUDGET_SECONDS
        diagram_count = 0
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
                rendered = _diagram_html(
                    "\n".join(block[1:-1]),
                    deadline=deadline,
                    allowed=diagram_count < MAX_REPORT_DIAGRAMS,
                )
                diagram_count += 1
                placeholder = self.md.htmlStash.store(rendered)
                out.extend(["", placeholder, ""])
            else:
                out.extend(block)
            index = end + 1
        return out


class DiagramExtension(Extension):  # type: ignore[misc]
    def extendMarkdown(self, md: Markdown) -> None:  # noqa: N802
        # Ahead of fenced_code (25), which would turn the block into code.
        md.preprocessors.register(_DiagramPreprocessor(md), "graphviz_diagram", 30)
