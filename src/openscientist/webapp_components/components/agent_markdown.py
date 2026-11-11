"""Markdown for agent-authored content embedded in application pages."""

from urllib.parse import urlsplit

import nh3
from nicegui import ui


def _filter_url(tag: str, attribute: str, value: str) -> str | None:
    if tag == "a" and attribute == "href":
        try:
            scheme = urlsplit(value).scheme.lower()
        except ValueError:
            return None
        return value if scheme in {"", "http", "https", "mailto"} else None
    if tag == "img" and attribute == "src":
        if value.startswith("data:image/"):
            return value
        if value.startswith("/web/jobs/") and "\\" not in value:
            return value
        return None
    return value


_CLEANER = nh3.Cleaner(
    tags={
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "br",
        "hr",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "pre",
        "code",
        "blockquote",
        "em",
        "strong",
        "b",
        "i",
        "s",
        "del",
        "sub",
        "sup",
        "a",
        "img",
        "div",
        "span",
    },
    clean_content_tags={"script", "style", "form", "input", "button", "textarea", "select"},
    attributes={
        "a": {"href", "title"},
        "img": {"src", "alt", "title"},
        "th": {"colspan", "rowspan", "scope"},
        "td": {"colspan", "rowspan"},
        "ol": {"start"},
    },
    allowed_classes={"div": {"codehilite"}},
    attribute_filter=_filter_url,
    url_schemes={"http", "https", "mailto", "data"},
    link_rel="noopener noreferrer",
)


def render_agent_markdown(content: str) -> ui.markdown:
    """Render agent text without styles, forms, scripts or remote images."""
    return ui.markdown(content, sanitize=_CLEANER.clean)
