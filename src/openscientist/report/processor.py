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
