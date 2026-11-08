"""Viewing copies of the images a job was given.

The agent looks at an image by reading it into its conversation. A copy no
larger than the model sees keeps that read small whatever the upload weighs,
while the original in ``data/`` stays untouched for code that analyses it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

#: The job directory's folder of viewing copies, named after the originals.
PREVIEW_DIR = "previews"
#: The image formats the agent's Read tool shows.
VIEWABLE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
#: The longest edge the Messages API keeps. It scales larger images down to
#: this itself, so the model sees nothing a copy at this size leaves out.
MAX_EDGE_PX = 1568
_JPEG_QUALITY = 85


def preview_name(original: str) -> str:
    """The viewing copy's file name, unique per original even when two
    originals share a stem."""
    return f"{original}.jpg"


def write_previews(data_paths: list[Path], job_dir: Path) -> list[Path]:
    """A JPEG viewing copy of each viewable image, at most ``MAX_EDGE_PX`` on
    its longest edge, in ``job_dir/previews``. An image Pillow cannot open
    gets none, and its original stays the only copy."""
    previews: list[Path] = []
    for original in data_paths:
        if original.suffix.lower() not in VIEWABLE_SUFFIXES:
            continue
        try:
            with Image.open(original) as image:
                image.seek(0)
                copy = ImageOps.exif_transpose(image)
                copy.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX))
                if copy.mode in ("RGBA", "LA", "P"):
                    rgba = copy.convert("RGBA")
                    copy = Image.new("RGB", rgba.size, "white")
                    copy.paste(rgba, mask=rgba.getchannel("A"))
                copy = copy.convert("RGB")
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            logger.warning("No viewing copy for %s: %s", original.name, type(error).__name__)
            continue
        target = job_dir / PREVIEW_DIR / preview_name(original.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        copy.save(target, "JPEG", quality=_JPEG_QUALITY, optimize=True)
        previews.append(target)
    return previews
