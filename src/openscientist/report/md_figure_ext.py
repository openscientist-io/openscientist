"""Markdown extension for figure processing.

Provides two processors:
- ``FigureBlockProcessor``: converts ``{{figure:...}}`` tags to ``<figure>`` elements
- ``ImageSrcTreeProcessor``: resolves relative image paths in ``![alt](path)`` syntax

This replaces the regex-based ``process_figure_tags()`` approach, handling figure
tags at the correct stage of the markdown pipeline.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as etree  # noqa: N813
from pathlib import Path

from markdown import Markdown
from markdown.blockprocessors import BlockProcessor
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

from openscientist.report.processor import _parse_params, _resolve_image_src

logger = logging.getLogger(__name__)

_FIGURE_START_RE = re.compile(r"^(?! {4}|\t)\s*\{\{figure:")


class FigureBlockProcessor(BlockProcessor):
    """Convert ``{{figure:filename|caption=...|width=...}}`` blocks to <figure> elements."""

    RE = re.compile(
        r"^(?! {4}|\t)\s*\{\{figure:(?P<filename>[^|}\s]+)"
        r"(?P<params>(?:\|[^}]*)?)"
        r"\}\}\s*$",
        re.MULTILINE,
    )

    def __init__(
        self,
        parser: object,
        provenance_dir: Path,
        use_base64: bool,
    ) -> None:
        super().__init__(parser)
        self.provenance_dir = provenance_dir
        self.use_base64 = use_base64

    def test(self, parent: etree.Element, block: str) -> bool:
        return bool(_FIGURE_START_RE.match(block))

    def run(self, parent: etree.Element, blocks: list[str]) -> bool:
        block = blocks[0]
        m = self.RE.match(block)
        if not m:
            return False

        # Consume the block
        blocks.pop(0)

        filename = m.group("filename")
        params = _parse_params(m.group("params"))
        caption = params.get("caption", "")
        width = params.get("width", "")

        image_path = self.provenance_dir / filename
        if not image_path.exists():
            logger.warning("Figure not found: %s", image_path)
            if caption:
                p = etree.SubElement(parent, "p")
                p.text = f"[Figure: {caption}]"
            return True

        src = _resolve_image_src(image_path, self.use_base64)

        figure = etree.SubElement(parent, "figure")
        if width:
            figure.set("style", f"max-width: {width}")

        img = etree.SubElement(figure, "img")
        img.set("src", src)
        img.set("alt", caption)

        if caption:
            figcaption = etree.SubElement(figure, "figcaption")
            figcaption.text = caption

        return True


class ImageSrcTreeProcessor(Treeprocessor):
    """Resolve relative image paths and wrap in <figure> elements."""

    def __init__(
        self,
        md: Markdown,
        provenance_dir: Path,
        use_base64: bool,
    ) -> None:
        super().__init__(md)
        self.provenance_dir = provenance_dir
        self.use_base64 = use_base64

    def run(self, root: etree.Element) -> etree.Element | None:
        for parent in root.iter():
            # Collect children to modify (can't modify during iteration)
            replacements: list[tuple[int, etree.Element, etree.Element]] = []
            for i, child in enumerate(parent):
                if child.tag != "img":
                    continue
                src = child.get("src", "")
                if not src or src.startswith(
                    ("http://", "https://", "data:", "file://")
                ):
                    continue

                # Try resolving relative to provenance dir
                image_path = self.provenance_dir / Path(src).name
                if not image_path.exists():
                    image_path = self.provenance_dir.parent / src
                    if not image_path.exists():
                        continue

                resolved = _resolve_image_src(image_path, self.use_base64)
                alt = child.get("alt", "")

                # Build <figure> wrapper
                figure = etree.Element("figure")
                img = etree.SubElement(figure, "img")
                img.set("src", resolved)
                img.set("alt", alt)
                if alt:
                    figcaption = etree.SubElement(figure, "figcaption")
                    figcaption.text = alt

                replacements.append((i, child, figure))

            # Apply replacements in reverse order to preserve indices
            for i, old, new in reversed(replacements):
                # Copy tail text to new element
                new.tail = old.tail
                parent.remove(old)
                parent.insert(i, new)

        return None


class FigureExtension(Extension):
    """Markdown extension for figure tag processing and image path resolution."""

    def __init__(self, **kwargs: object) -> None:
        self.config = {
            "provenance_dir": [".", "Directory containing figure images"],
            "use_base64": [False, "Embed images as base64 data URIs"],
        }
        super().__init__(**kwargs)

    def extendMarkdown(self, md: Markdown) -> None:  # noqa: N802
        provenance_dir = Path(str(self.getConfig("provenance_dir")))
        use_base64 = bool(self.getConfig("use_base64"))

        md.parser.blockprocessors.register(
            FigureBlockProcessor(md.parser, provenance_dir, use_base64),
            "figure_block",
            105,
        )

        md.treeprocessors.register(
            ImageSrcTreeProcessor(md, provenance_dir, use_base64),
            "image_src_resolve",
            5,
        )
