"""The HTML vocabulary of the formatted research question.

The agent's first turn writes the question as an HTML fragment in this
vocabulary, and the job page keeps only this vocabulary when it renders the
fragment, so the prompt that asks for it and the sanitizer that enforces it
read the same lists.
"""

from __future__ import annotations

from pathlib import PurePosixPath

#: Attached files a browser shows inline. TIFF and other scientific image
#: formats are listed as files, because no browser renders them.
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}
)

#: Job-relative directory the attached files live in. The fragment names an
#: attachment as ``data/<file name>``.
ATTACHMENT_DIR = "data"

ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a",
        "abbr",
        "b",
        "blockquote",
        "br",
        "caption",
        "code",
        "dd",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "hr",
        "i",
        "img",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "s",
        "section",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)

#: Attributes kept on every tag. ``class`` keeps only ``LAYOUT_CLASSES``, and
#: ``lang`` only a well-formed language tag, which justified text needs for
#: the browser to hyphenate it.
GLOBAL_ATTRIBUTES: frozenset[str] = frozenset({"class", "lang"})

#: Attributes kept per tag besides ``GLOBAL_ATTRIBUTES``. ``href`` takes an
#: http, https or mailto URL or an attachment path, and ``src`` only an
#: attachment path.
ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "abbr": frozenset({"title"}),
    "img": frozenset({"src", "alt", "title"}),
    "ol": frozenset({"start"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan"}),
}

#: Layout classes the fragment may use, each with what it does.
LAYOUT_CLASSES: dict[str, str] = {
    "fp-float-left": "float left with text wrapping around it (figures, images)",
    "fp-float-right": "float right with text wrapping around it (figures, images)",
    "fp-center": "centre a block (figures, images, tables)",
    "fp-clear": "start below any floated element",
    "fp-small": "about a quarter of the width (figures, images)",
    "fp-medium": "about half of the width (figures, images)",
    "fp-large": "the full width (figures, images)",
    "fp-justify": "justify the text of a paragraph or block",
    "fp-text-center": "centre the text",
    "fp-text-right": "align the text right",
    "fp-lead": "a larger introductory paragraph",
    "fp-note": "a highlighted call-out box",
    "fp-muted": "de-emphasised grey text",
}


#: The Font Awesome solid icons the formatting turn may mark the question's
#: parts with, each with what it marks. The sanitizer keeps an ``i`` element
#: only with exactly ``fa-solid`` and one of these, and the prompt offers
#: this same list.
PROMPT_ICONS: dict[str, str] = {
    "bullseye": "the aim or the central question",
    "circle-question": "a hypothesis or an untested question",
    "database": "the data or a dataset",
    "magnifying-glass": "the method or a search",
    "triangle-exclamation": "a caveat or a limitation",
    "book-open": "the literature or a prior result",
    "clock": "the timeline or a deadline",
}


def is_inline_image(file_name: str) -> bool:
    """Whether an attached file is an image a browser shows inline."""
    return PurePosixPath(file_name).suffix.lower() in IMAGE_EXTENSIONS
