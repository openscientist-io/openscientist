"""Agent markdown preserves formatting without active HTML."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.elements.markdown import prepare_content
from nicegui.testing.user_simulation import user_simulation
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, JobChatMessage, User
from openscientist.report.icons import REPORT_ICONS
from openscientist.webapp_components.components.agent_markdown import _CLEANER
from openscientist.webapp_components.components.badges import transform_pmid_references
from openscientist.webapp_components.pages.job_detail.chat import _ChatTabController
from openscientist.webapp_components.pages.job_detail.common import _JobDetailContext
from openscientist.webapp_components.pages.job_detail.report import _render_report_markdown

_PAYLOAD = """# Findings

<style>body { display: none; }</style>
<form action="https://example.org/submit"><input name="password"><button>Sign in</button>
<textarea>Secret</textarea><select><option>Secret</option></select></form>
<script>alert('secret')</script>
<p style="position:fixed" onclick="alert(1)">Safe paragraph</p>
<a href="javascript:alert(1)">Unsafe link</a>
<img src="https://example.org/remote" onerror="alert(1)">

| Result | Value |
| --- | --- |
| Count | 42 |

```python
print("result")
```

[Reference](https://example.org/paper)

[Contact](mailto:science@example.org)

[Local](/web/jobs/example/report.html)

![Plot](/web/jobs/example/files/plot.png)

![Embedded](data:image/png;base64,iVBORw0KGgo=)
"""


class _Fragment(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


@pytest.mark.asyncio
@pytest.mark.parametrize("renderer", ["assistant", "report"])
async def test_agent_markdown_removes_active_html_and_preserves_markdown(
    renderer: str, db_session: AsyncSession, webapp_user: User, tmp_path: Path
) -> None:
    job = Job(owner_id=webapp_user.id, research_question="Safe rendering", status="completed")
    db_session.add(job)
    await db_session.flush()
    message = JobChatMessage(job_id=job.id, role="assistant", content=_PAYLOAD)
    db_session.add(message)
    await db_session.flush()
    context = _JobDetailContext(
        job_id=str(job.id),
        user_id=str(webapp_user.id),
        job_manager=None,
        job_info=job,
        is_owner=True,
        can_edit=True,
        owner_name=None,
        job_dir=tmp_path,
        ks_data=None,
        action_counts={},
        ks_load_error=None,
        spend=0,
        spend_visible=False,
        state={},
        share_dialog=None,
        delete_dialog=None,
        notifications_dialog=None,
        attachments=[],
    )
    report_path = tmp_path / "final_report.md"
    report_path.write_text(message.content)

    async def page() -> None:
        if renderer == "assistant":
            _ChatTabController(context)._render_message_bubble(message.role, message.content)
        else:
            await _render_report_markdown(ui.column(), report_path)

    async with user_simulation(root=page) as browser:
        client = await browser.open("/")
        await browser.should_see("Findings")
        [markdown] = [e for e in client.elements.values() if isinstance(e, ui.markdown)]
        fragment = _Fragment(markdown.props["innerHTML"])
        tags = {tag for tag, _ in fragment.elements}
        assert not tags.intersection(
            {"style", "form", "input", "button", "textarea", "select", "script"}
        )
        assert {"h1", "p", "table", "th", "td", "pre", "code", "a", "img"} <= tags
        for _, attrs in fragment.elements:
            assert "style" not in attrs
            assert not any(name.startswith("on") for name in attrs)
            assert not (attrs.get("href") or "").startswith("javascript:")
        links = [attrs for tag, attrs in fragment.elements if tag == "a"]
        for href in (
            "https://example.org/paper",
            "mailto:science@example.org",
            "/web/jobs/example/report.html",
        ):
            [link] = [attrs for attrs in links if attrs.get("href") == href]
            assert "noopener" in (link.get("rel") or "").split()
        image_sources = {attrs.get("src") for tag, attrs in fragment.elements if tag == "img"}
        assert "https://example.org/remote" not in image_sources
        assert "/web/jobs/example/files/plot.png" in image_sources
        assert "data:image/png;base64,iVBORw0KGgo=" in image_sources


def test_highlighted_code_keeps_token_classes() -> None:
    html = prepare_content(
        '```python\nprint("result", 42)\n```', extras="fenced-code-blocks tables"
    )
    expected = [
        attrs["class"]
        for tag, attrs in _Fragment(html).elements
        if tag == "span" and attrs.get("class")
    ]
    assert expected
    cleaned = _Fragment(_CLEANER.clean(html))
    assert [
        attrs["class"] for tag, attrs in cleaned.elements if tag == "span" and attrs.get("class")
    ] == expected
    assert ("div", {"class": "codehilite"}) in cleaned.elements


def test_aligned_table_keeps_only_text_alignment() -> None:
    html = prepare_content(
        "| Left | Center | Right |\n| :--- | :---: | ---: |\n| a | b | c |", extras="tables"
    )
    html = html.replace('style="', 'style="position:fixed;color:red;')
    cells = [
        attrs for tag, attrs in _Fragment(_CLEANER.clean(html)).elements if tag in {"th", "td"}
    ]
    assert [attrs.get("style") for attrs in cells] == [
        f"text-align:{alignment}" for alignment in ("left", "center", "right") * 2
    ]


@pytest.mark.parametrize("icon", REPORT_ICONS)
def test_report_icons_and_pubmed_badges_keep_their_markup(icon: str) -> None:
    html = prepare_content(
        transform_pmid_references(f'<i class="fa-solid fa-{icon}"></i> PMID: 12345678'),
        extras="fenced-code-blocks tables",
    )
    expected = [
        (tag, {name: value for name, value in attrs.items() if name != "style"})
        for tag, attrs in _Fragment(html).elements
    ]
    cleaned = _CLEANER.clean(html)
    assert _Fragment(cleaned).elements == expected
    assert ">P</text>" in cleaned
    assert ">12345678</a>" in cleaned
    [text] = [attrs for tag, attrs in _Fragment(cleaned).elements if tag == "text"]
    assert text["fill"] == "white"
    assert text["font-size"] == "11px"
    assert text["font-weight"] == "bold"
    assert text["font-family"] == "Arial,sans-serif"


def test_svg_removes_active_content_and_external_references() -> None:
    html = prepare_content(
        '<svg onload="alert(1)" href="https://example.org/icon" '
        'xlink:href="https://example.org/icon">'
        '<rect x="1" fill="url(https://example.org/paint)" onclick="alert(1)"/>'
        '<text x="8" href="javascript:alert(1)" xlink:href="https://example.org/text">P</text>'
        "<foreignObject><p>Embedded content</p></foreignObject>"
        "<script>alert(1)</script></svg>",
        extras="tables",
    )
    cleaned = _CLEANER.clean(html)
    elements = _Fragment(cleaned).elements
    assert {"svg", "rect", "text"} <= {tag for tag, _ in elements}
    assert not {"foreignobject", "script"} & {tag for tag, _ in elements}
    assert "Embedded content" not in cleaned
    assert "alert(1)" not in cleaned
    for tag, attrs in elements:
        assert not any(name.startswith("on") for name in attrs)
        if tag in {"svg", "rect", "text"}:
            assert not {"href", "xlink:href", "fill"} & attrs.keys()


def test_unrecognized_formatting_classes_are_removed() -> None:
    html = '<div class="overlay"><span class="overlay">Text</span><i class="fa-unknown"></i></div>'
    assert all(not attrs.get("class") for _, attrs in _Fragment(_CLEANER.clean(html)).elements)
