"""The research question as the job page shows it.

The model formats the question into an HTML fragment at job start. That
fragment comes from an agent-writable column, so it is rendered only after
``sanitize_formatted_prompt`` has cut it down to the vocabulary of
``openscientist.formatted_prompt`` and pointed its attachment paths at the
job's file route. The original text stays one click away.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit

import nh3
from nicegui import ui

from openscientist.formatted_prompt import (
    ALLOWED_ATTRIBUTES,
    ALLOWED_TAGS,
    ATTACHMENT_DIR,
    GLOBAL_ATTRIBUTES,
    LAYOUT_CLASSES,
    PROMPT_ICONS,
    is_inline_image,
)
from openscientist.webapp_components.components.common import FONTAWESOME_HEAD_HTML

_LINK_SCHEMES = frozenset({"http", "https", "mailto"})

#: A BCP 47 language tag such as ``en``, ``en-GB`` or ``zh-Hant-TW``.
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")

#: The language the browser hyphenates justified text in when the fragment
#: declares none.
_DEFAULT_LANGUAGE = "en"

#: A link whose content is text only, in the sanitizer's normalized output.
_PLAIN_LINK = re.compile(r"(<a\b[^>]*>)([^<]+)(</a>)")
#: A separator inside a DOI or URL that a line may wrap after.
_LINK_SEPARATOR = re.compile(r"([/:._=?-])(?=\w)")

#: Above this weight the prompt starts collapsed behind Show more. The weight
#: is the visible text length plus ``_IMAGE_WEIGHT`` per image, which stands
#: for the lines of text an image displaces.
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
.os-prompt { font-size: 1rem; line-height: 1.6; color: #1f2937; overflow-wrap: anywhere; }
.os-prompt::after { content: ""; display: table; clear: both; }
.os-prompt > :first-child,
.os-prompt :is(h1, h2, h3, h4):first-child { margin-top: 0; }
.os-prompt :is(h1, h2, h3, h4) { line-height: 1.3; letter-spacing: normal; }
.os-prompt h1, .os-prompt h2 { font-size: 1.05rem; font-weight: 700; margin: .75rem 0 .25rem; }
.os-prompt h3 { font-size: 1rem; font-weight: 700; margin: .6rem 0 .2rem; }
.os-prompt h4 { font-size: .95rem; font-weight: 600; margin: .5rem 0 .2rem; }
.os-prompt p { margin: 0 0 .5rem; }
.os-prompt ul, .os-prompt ol { margin: 0 0 .5rem; padding-left: 1.5rem; }
.os-prompt ul { list-style: disc; }
.os-prompt ol { list-style: decimal; }
.os-prompt li { margin: .15rem 0; }
.os-prompt li > ul { list-style: circle; margin-bottom: 0; }
.os-prompt dt { font-weight: 600; }
.os-prompt dd { margin: 0 0 .5rem 1.5rem; }
.os-prompt a { color: #2563eb; text-decoration: underline; }
.os-prompt blockquote { margin: 0 0 .75rem; padding: .25rem 1rem;
  border-left: 4px solid #d1d5db; color: #4b5563; }
.os-prompt code { font-family: ui-monospace, monospace; font-size: .9em;
  background: #f3f4f6; padding: .1rem .3rem; border-radius: .25rem; }
.os-prompt pre { background: #f3f4f6; padding: .75rem; border-radius: .375rem;
  overflow-x: auto; margin: 0 0 .75rem; }
.os-prompt pre code { background: none; padding: 0; }
.os-prompt table { border-collapse: collapse; margin: 0 0 .75rem; font-size: .95rem; }
.os-prompt th, .os-prompt td { border: 1px solid #e5e7eb; padding: .3rem .6rem; text-align: left; }
.os-prompt th { background: #f9fafb; font-weight: 600; }
.os-prompt caption { caption-side: bottom; font-size: .85rem; color: #6b7280; padding-top: .3rem; }
.os-prompt hr { border: 0; border-top: 1px solid #e5e7eb; margin: 1rem 0; }
.os-prompt mark { background: #fef08a; padding: 0 .15rem; }
.os-prompt i.fa-solid { margin-right: .4em; }
.os-prompt figure { margin: 0 0 .75rem; }
.os-prompt img { max-width: 100%; max-height: 24rem; height: auto; border-radius: .25rem; }
.os-prompt figure img { display: block; margin-inline: auto; }
.os-prompt figcaption { font-size: .85rem; color: #6b7280; margin-top: .25rem; text-align: center; }
.os-prompt .fp-float-left { float: left; margin: .25rem 1rem .5rem 0; max-width: 50%; }
.os-prompt .fp-float-right { float: right; margin: .25rem 0 .5rem 1rem; max-width: 50%; }
.os-prompt .fp-center { display: block; margin-left: auto; margin-right: auto; }
.os-prompt .fp-clear { clear: both; }
.os-prompt .fp-small { width: 25%; min-width: 8rem; }
.os-prompt .fp-medium { width: 50%; min-width: 12rem; }
.os-prompt .fp-large { width: 100%; max-width: 100%; float: none; margin-left: 0; margin-right: 0; }
.os-prompt .fp-justify { text-align: justify; text-wrap: pretty; -webkit-hyphens: auto;
  hyphens: auto; hyphenate-limit-chars: 6 3 3; }
.os-prompt .fp-text-center { text-align: center; }
.os-prompt .fp-text-right { text-align: right; }
.os-prompt .fp-lead { font-size: 1.1rem; color: #374151; }
.os-prompt .fp-note { background: #eff6ff; border-left: 4px solid #3b82f6;
  padding: .5rem .75rem; border-radius: .25rem; margin: 0 0 .75rem; }
.os-prompt .fp-muted { color: #6b7280; }
@media (max-width: 640px) {
  .os-prompt .fp-float-left, .os-prompt .fp-float-right { float: none; max-width: 100%;
    width: 100%; margin: 0 0 .75rem; }
}
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


@dataclass(frozen=True)
class SanitizedPrompt:
    """The renderable fragment and the attached images it already shows."""

    html: str
    placed_images: frozenset[str]


def attachment_url(job_id: str, file_path: str) -> str:
    """The job file route serving an attachment at its job-relative path."""
    return f"/web/jobs/{job_id}/files/{quote(file_path)}"


def _attachment_name(value: str) -> str | None:
    """The file name an attachment path names, or None for any other URL."""
    parts = urlsplit(value.strip())
    if parts.scheme or parts.netloc or parts.query or parts.fragment:
        return None
    path = PurePosixPath(unquote(parts.path))
    if len(path.parts) != 2 or path.parts[0] != ATTACHMENT_DIR:
        return None
    return path.name


def _is_web_link(value: str) -> bool:
    scheme = urlsplit(value.strip()).scheme.lower()
    return scheme in _LINK_SCHEMES


def sanitize_formatted_prompt(
    fragment: str, *, attachment_urls: Mapping[str, str]
) -> SanitizedPrompt:
    """Keep only the formatted-prompt vocabulary of ``fragment``.

    ``attachment_urls`` maps each attachment the viewer may open to its URL.
    An image keeps its source only when it names an attached image, a link
    only when it is an http, https or mailto URL or names an attachment, and
    an ``i`` element only with exactly ``fa-solid`` plus one of
    ``PROMPT_ICONS``. An icon that loses its class leaves an empty ``i``,
    which the fragment drops.
    """
    placed: set[str] = set()

    def icon_classes(value: str) -> str | None:
        tokens = set(value.split())
        if len(tokens) != 2 or "fa-solid" not in tokens:
            return None
        (icon,) = tokens - {"fa-solid"}
        if icon.removeprefix("fa-") not in PROMPT_ICONS or not icon.startswith("fa-"):
            return None
        return f"fa-solid {icon}"

    def keep(tag: str, attribute: str, value: str) -> str | None:
        if attribute == "class":
            if tag == "i":
                return icon_classes(value)
            kept = [token for token in value.split() if token in LAYOUT_CLASSES]
            return " ".join(kept) or None
        if attribute == "lang":
            return value if _LANGUAGE_TAG.fullmatch(value) else None
        if tag == "img" and attribute == "src":
            name = _attachment_name(value)
            if name is None or name not in attachment_urls or not is_inline_image(name):
                return None
            placed.add(name)
            return attachment_urls[name]
        if tag == "a" and attribute == "href":
            if _is_web_link(value):
                return value
            name = _attachment_name(value)
            return attachment_urls.get(name) if name is not None else None
        return value

    attributes = {tag: set(names) for tag, names in ALLOWED_ATTRIBUTES.items()}
    attributes["*"] = set(GLOBAL_ATTRIBUTES)
    cleaned = nh3.clean(
        fragment,
        tags=set(ALLOWED_TAGS),
        clean_content_tags={"script", "style"},
        attributes=attributes,
        attribute_filter=keep,
        url_schemes=set(_LINK_SCHEMES),
        link_rel="noopener noreferrer nofollow",
        set_tag_attribute_values={"a": {"target": "_blank"}},
    )
    # An icon that lost its class is an empty <i>, dropped here. An <i> with
    # text is plain emphasis and stays.
    cleaned = re.sub(r"<i>\s*</i>\s?", "", cleaned)
    return SanitizedPrompt(html=_with_link_breaks(cleaned), placed_images=frozenset(placed))


def _with_link_breaks(cleaned: str) -> str:
    """Let a DOI or URL wrap after its separators, the way a hyphen lets a
    word wrap, so it does not leave wide gaps in a justified line."""

    def add_breaks(match: re.Match[str]) -> str:
        opening, text, closing = match.groups()
        if any(c.isspace() for c in text):
            return match.group(0)
        return opening + _LINK_SEPARATOR.sub(r"\1<wbr>", text) + closing

    return _PLAIN_LINK.sub(add_breaks, cleaned)


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


#: A tag that starts or ends a block, so the text on either side reads as
#: separate words.
_BLOCK_TAG = re.compile(
    r"</?(?:blockquote|br|caption|dd|div|dl|dt|figcaption|figure|h[1-6]|hr|li|ol|p|pre"
    r"|section|table|tbody|td|tfoot|th|thead|tr|ul)\b[^>]*>"
)


def _collapse_space(text: str) -> str:
    return " ".join(text.split())


def _plain_text(fragment: str) -> str:
    """The words a sanitized fragment shows, with whitespace collapsed."""
    spaced = _BLOCK_TAG.sub(" ", fragment)
    return _collapse_space(html.unescape(nh3.clean(spaced, tags=set())))


def _render_gallery(images: list[Attachment]) -> None:
    figures = "".join(
        f'<a href="{html.escape(image.url)}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{html.escape(image.url)}" alt="{html.escape(image.name)}" '
        f'title="{html.escape(image.name)}"></a>'
        for image in images
    )
    # Built here from escaped attachment URLs and names, never from model output.
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


def _toggle_original(original: ui.label, button: ui.button) -> None:
    original.set_visibility(not original.visible)
    button.set_text("Hide original prompt" if original.visible else "Show original prompt")


def add_prompt_head_html() -> None:
    """The prompt's styles and the Font Awesome icons of its attachments."""
    ui.add_head_html(_PROMPT_CSS + FONTAWESOME_HEAD_HTML)


def render_research_prompt(
    research_question: str,
    formatted_prompt: str | None,
    attachments: list[Attachment],
) -> None:
    """The formatted prompt, or the original text while there is none, then
    the images it does not show, a Show more for a long prompt, the original
    text behind a toggle and the other attachments."""
    images = [a for a in attachments if is_inline_image(a.name)]
    files = [a for a in attachments if not is_inline_image(a.name)]
    sanitized = (
        sanitize_formatted_prompt(
            formatted_prompt, attachment_urls={a.name: a.url for a in attachments}
        )
        if formatted_prompt
        else None
    )
    placed = sanitized.placed_images if sanitized is not None else frozenset()
    unplaced = [image for image in images if image.name not in placed]
    text = _plain_text(sanitized.html) if sanitized else _collapse_space(research_question)
    # The original adds nothing when the formatting changed only markup.
    offers_original = sanitized is not None and text != _collapse_space(research_question)

    with ui.column().classes("w-full gap-1"):
        with ui.element("div").classes("os-prompt-body w-full") as body:
            if sanitized is not None:
                # Sanitized above against the formatted-prompt vocabulary.
                ui.html(sanitized.html, sanitize=False).classes("os-prompt").props(
                    f"lang={_DEFAULT_LANGUAGE}"
                )
            else:
                ui.label(research_question).classes(
                    "os-prompt-original text-base whitespace-pre-wrap"
                )
            if unplaced:
                _render_gallery(unplaced)
        if _is_long(len(text), len(placed) + len(unplaced)):
            body.classes(add="os-prompt-collapsed")
            more = ui.button("Show more").props("flat dense no-caps color=primary")
            more.on_click(lambda: _toggle_collapsed(body, more))
        if offers_original:
            show = ui.button("Show original prompt").props("flat dense no-caps color=grey")
            show.classes("self-start text-xs")
            original = ui.label(research_question).classes(
                "os-prompt-original w-full whitespace-pre-wrap text-sm text-gray-600 "
                "bg-gray-50 border rounded p-3"
            )
            original.set_visibility(False)
            show.on_click(lambda: _toggle_original(original, show))
        if files:
            _render_attachments(files)
