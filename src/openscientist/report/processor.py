"""Figure tag post-processor.

Parses ``{{figure:filename|caption=...|width=...}}`` tags in markdown and
replaces them with HTML ``<figure>`` elements (or plain-text fallbacks).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches {{figure:filename.png|caption=...|width=...}}
_FIGURE_TAG_RE = re.compile(
    r"\{\{figure:(?P<filename>[^|}\s]+)"  # filename (required)
    r"(?P<params>(?:\|[^}]*)?)"  # optional |key=value params
    r"\}\}"
)


def _parse_params(raw: str) -> dict[str, str]:
    """Parse ``|key=value|key2=value2`` into a dict."""
    params: dict[str, str] = {}
    if not raw:
        return params
    for part in raw.lstrip("|").split("|"):
        if "=" in part:
            key, value = part.split("=", 1)
            params[key.strip()] = value.strip()
    return params


def process_figure_tags(
    markdown: str,
    provenance_dir: Path,
    *,
    use_base64: bool = False,
) -> str:
    """Replace ``{{figure:...}}`` tags with HTML ``<figure>`` elements.

    Also normalizes standard markdown image syntax ``![alt](path)`` to
    resolve against the provenance directory.

    Args:
        markdown: Raw markdown with figure tags.
        provenance_dir: Directory containing plot PNG files.
        use_base64: If True, embed images as base64 data URIs.

    Returns:
        Markdown with figure tags replaced by HTML ``<figure>`` elements.
    """

    def _replace_tag(match: re.Match[str]) -> str:
        filename = match.group("filename")
        params = _parse_params(match.group("params"))
        caption = params.get("caption", "")
        width = params.get("width", "")

        image_path = provenance_dir / filename
        if not image_path.exists():
            logger.warning("Figure not found: %s", image_path)
            if caption:
                return f"\n\n[Figure: {caption}]\n\n"
            return ""

        src = _resolve_image_src(image_path, use_base64)
        width_attr = f' style="max-width: {width}"' if width else ""
        caption_html = f"\n  <figcaption>{caption}</figcaption>" if caption else ""

        return (
            f"\n\n<figure{width_attr}>\n"
            f'  <img src="{src}" alt="{caption}">{caption_html}\n'
            f"</figure>\n\n"
        )

    result = _FIGURE_TAG_RE.sub(_replace_tag, markdown)

    # Also normalize standard markdown images: ![alt](filename.png)
    result = _normalize_markdown_images(result, provenance_dir, use_base64)

    return result


def _normalize_markdown_images(
    markdown: str,
    provenance_dir: Path,
    use_base64: bool,
) -> str:
    """Resolve relative image paths in standard ``![alt](path)`` syntax."""
    img_re = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

    def _replace_img(match: re.Match[str]) -> str:
        alt = match.group(1)
        path_str = match.group(2)

        # Skip URLs and absolute paths
        if path_str.startswith(("http://", "https://", "data:", "file://")):
            return match.group(0)

        # Try resolving relative to provenance dir
        image_path = provenance_dir / Path(path_str).name
        if not image_path.exists():
            # Try as-is from job_dir (parent of provenance)
            image_path = provenance_dir.parent / path_str
            if not image_path.exists():
                return match.group(0)

        src = _resolve_image_src(image_path, use_base64)
        return f'<figure>\n  <img src="{src}" alt="{alt}">\n  <figcaption>{alt}</figcaption>\n</figure>'

    return img_re.sub(_replace_img, markdown)


def _resolve_image_src(image_path: Path, use_base64: bool) -> str:
    """Return image src attribute — either file:// URI or base64 data URI."""
    if use_base64:
        import base64

        data = image_path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:image/png;base64,{b64}"
    return image_path.as_uri()


def strip_figure_tags(markdown: str) -> str:
    """Replace ``{{figure:...}}`` tags with plain-text captions.

    Used for the fpdf2 fallback renderer which has no image support.

    Args:
        markdown: Raw markdown with figure tags.

    Returns:
        Markdown with tags replaced by ``[Figure: caption]`` text.
    """

    def _replace_tag(match: re.Match[str]) -> str:
        params = _parse_params(match.group("params"))
        caption = params.get("caption", match.group("filename"))
        return f"\n\n[Figure: {caption}]\n\n"

    return _FIGURE_TAG_RE.sub(_replace_tag, markdown)
