"""The research question as the job page shows it.

The question reads as the scientist wrote it, justified and hyphenated, with
the attached images below it and the other attached files as links. A long
question starts collapsed behind Show more.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote

from nicegui import ui

from openscientist.webapp_components.components.common import (
    DEFAULT_LANGUAGE,
    FONTAWESOME_HEAD_HTML,
)

#: Attached files a browser shows inline. TIFF and other scientific image
#: formats are listed as files, because no browser renders them.
_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
)


def _is_inline_image(file_name: str) -> bool:
    """Whether an attached file is an image a browser shows inline."""
    return PurePosixPath(file_name).suffix.lower() in _IMAGE_EXTENSIONS


#: Above this weight the question starts collapsed behind Show more. The
#: weight is the question's length plus ``_IMAGE_WEIGHT`` per image, which
#: stands for the lines of text an image displaces.
_COLLAPSE_WEIGHT = 2500
_IMAGE_WEIGHT = 400

_FILE_ICONS: dict[str, str] = {
    ".pdf": "fa-file-pdf",
    ".csv": "fa-file-csv",
    ".tsv": "fa-file-csv",
    ".xls": "fa-file-excel",
    ".xlsx": "fa-file-excel",
    ".doc": "fa-file-word",
    ".docx": "fa-file-word",
    ".ppt": "fa-file-powerpoint",
    ".pptx": "fa-file-powerpoint",
    ".zip": "fa-file-zipper",
    ".gz": "fa-file-zipper",
    ".tgz": "fa-file-zipper",
    ".tar": "fa-file-zipper",
    ".bz2": "fa-file-zipper",
    ".xz": "fa-file-zipper",
    ".7z": "fa-file-zipper",
    ".txt": "fa-file-lines",
    ".md": "fa-file-lines",
    ".rtf": "fa-file-lines",
    ".py": "fa-file-code",
    ".r": "fa-file-code",
    ".ipynb": "fa-file-code",
    ".json": "fa-file-code",
    ".xml": "fa-file-code",
    ".yaml": "fa-file-code",
    ".yml": "fa-file-code",
    ".sh": "fa-file-code",
    ".tif": "fa-file-image",
    ".tiff": "fa-file-image",
}

_PROMPT_CSS = """
<style>
.os-prompt-question { text-align: justify; text-wrap: pretty; -webkit-hyphens: auto;
  hyphens: auto; hyphenate-limit-chars: 6 3 3; }
.os-prompt-body { position: relative; }
.os-prompt-collapsed { max-height: 28rem; overflow: hidden; }
.os-prompt-collapsed::after { content: ""; position: absolute; left: 0; right: 0; bottom: 0;
  height: 4rem; pointer-events: none;
  background: linear-gradient(to bottom, rgba(255, 255, 255, 0), #fff); }
.os-prompt-gallery { display: flex; flex-wrap: wrap; gap: .75rem; margin-top: .75rem; }
.os-prompt-gallery img { max-height: 14rem; max-width: 100%; border-radius: .25rem;
  border: 1px solid #e5e7eb; }
.os-attachment { display: inline-flex; align-items: center; gap: .4rem; padding: .25rem .6rem;
  border: 1px solid #e5e7eb; border-radius: 9999px; color: #374151; text-decoration: none;
  font-size: .875rem; }
.os-attachment:hover { background: #f9fafb; }
.os-attachment i { color: #6b7280; }
</style>
"""


@dataclass(frozen=True)
class Attachment:
    """A file the scientist attached, as the viewer may open it."""

    name: str
    url: str
    size: int


def attachment_url(job_id: str, file_path: str) -> str:
    """The job file route serving an attachment at its job-relative path."""
    return f"/web/jobs/{job_id}/files/{quote(file_path)}"


def _file_icon(name: str) -> str:
    return _FILE_ICONS.get(PurePosixPath(name).suffix.lower(), "fa-file")


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _is_long(text_chars: int, images: int) -> bool:
    return text_chars + _IMAGE_WEIGHT * images > _COLLAPSE_WEIGHT


def _render_gallery(images: list[Attachment]) -> None:
    figures = "".join(
        f'<a href="{html.escape(image.url)}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{html.escape(image.url)}" alt="{html.escape(image.name)}" '
        f'title="{html.escape(image.name)}"></a>'
        for image in images
    )
    ui.html(figures, sanitize=False).classes("os-prompt-gallery")


def _render_attachments(files: list[Attachment]) -> None:
    with ui.row().classes("w-full gap-2 mt-3 items-center"):
        for file in files:
            with (
                ui.link(target=file.url, new_tab=True)
                .classes("os-attachment")
                .props(f'title="{html.escape(file.name)} ({_format_size(file.size)})"')
            ):
                ui.element("i").classes(f"fa-solid {_file_icon(file.name)}")
                ui.label(file.name)


def _toggle_collapsed(body: ui.element, button: ui.button) -> None:
    if "os-prompt-collapsed" in body.classes:
        body.classes(remove="os-prompt-collapsed")
        button.set_text("Show less")
    else:
        body.classes(add="os-prompt-collapsed")
        button.set_text("Show more")


def add_prompt_head_html() -> None:
    """The question card's styles and the Font Awesome icons of its attachments."""
    ui.add_head_html(_PROMPT_CSS + FONTAWESOME_HEAD_HTML)


def render_research_question(research_question: str, attachments: list[Attachment]) -> None:
    """The question as the scientist wrote it, then the attached images and
    the other attached files, with a Show more for a long question."""
    images = [a for a in attachments if _is_inline_image(a.name)]
    files = [a for a in attachments if not _is_inline_image(a.name)]

    with ui.column().classes("w-full gap-1"):
        with ui.element("div").classes("os-prompt-body w-full") as body:
            ui.label(research_question).classes(
                "os-prompt-question text-base whitespace-pre-wrap"
            ).props(f"lang={DEFAULT_LANGUAGE}")
            if images:
                _render_gallery(images)
        if _is_long(len(research_question), len(images)):
            body.classes(add="os-prompt-collapsed")
            more = ui.button("Show more").props("flat dense no-caps color=primary")
            more.on_click(lambda: _toggle_collapsed(body, more))
        if files:
            _render_attachments(files)
