"""The research question card of the job page: the model-formatted prompt,
sanitized, with the attached images placed or appended, the other attachments
listed under it, a toggle for the original text and a Show more for a long
prompt."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import AnalysisLog, Job, JobDataFile, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from openscientist.webapp_components.pages.job_detail.prompt import (
    attachment_url,
    sanitize_formatted_prompt,
)
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, WireMessage, drain_outbox

_JOB = "0190b6c4-0000-7000-8000-000000000001"
_URLS = {
    "fig 1.png": attachment_url(_JOB, "data/fig 1.png"),
    "counts.csv": attachment_url(_JOB, "data/counts.csv"),
}


def test_attachment_url_quotes_the_path() -> None:
    assert attachment_url(_JOB, "data/fig 1.png") == f"/web/jobs/{_JOB}/files/data/fig%201.png"


def test_scripts_handlers_styles_and_frames_are_removed() -> None:
    sanitized = sanitize_formatted_prompt(
        '<p style="position:fixed" onclick="steal()">Keep me</p>'
        "<script>alert(1)</script><iframe src='https://evil.test'></iframe>"
        '<img src="data/fig 1.png" onerror="steal()">',
        attachment_urls=_URLS,
    )
    assert "Keep me" in sanitized.html
    for gone in ("script", "alert", "iframe", "onclick", "onerror", "style", "steal"):
        assert gone not in sanitized.html


def test_an_attached_image_is_served_from_the_job_files() -> None:
    sanitized = sanitize_formatted_prompt(
        '<figure class="fp-float-right fp-medium"><img src="data/fig 1.png" alt="Gel">'
        "<figcaption>Gel</figcaption></figure>",
        attachment_urls=_URLS,
    )
    assert f'src="{_URLS["fig 1.png"]}"' in sanitized.html
    assert 'class="fp-float-right fp-medium"' in sanitized.html
    assert sanitized.placed_images == frozenset({"fig 1.png"})


def test_a_percent_encoded_or_dotted_attachment_path_resolves() -> None:
    sanitized = sanitize_formatted_prompt(
        '<img src="./data/fig%201.png" alt="Gel">', attachment_urls=_URLS
    )
    assert f'src="{_URLS["fig 1.png"]}"' in sanitized.html
    assert sanitized.placed_images == frozenset({"fig 1.png"})


@pytest.mark.parametrize(
    "src",
    [
        "https://tracker.test/pixel.png",
        "data/missing.png",
        "data/counts.csv",
        "/etc/passwd",
        "data/../secrets/fig 1.png",
        "javascript:alert(1)",
    ],
)
def test_an_image_outside_the_attached_images_loses_its_source(src: str) -> None:
    sanitized = sanitize_formatted_prompt(f'<img src="{src}" alt="x">', attachment_urls=_URLS)
    assert "src=" not in sanitized.html
    assert sanitized.placed_images == frozenset()


def test_links_open_in_a_new_tab_without_referrer() -> None:
    sanitized = sanitize_formatted_prompt(
        '<a href="https://doi.org/10.1000/xyz">DOI</a>', attachment_urls=_URLS
    )
    assert 'href="https://doi.org/10.1000/xyz"' in sanitized.html
    assert 'target="_blank"' in sanitized.html
    assert 'rel="noopener noreferrer nofollow"' in sanitized.html


def test_a_link_to_an_attachment_points_at_the_job_files() -> None:
    sanitized = sanitize_formatted_prompt(
        '<a href="data/counts.csv">counts</a>', attachment_urls=_URLS
    )
    assert f'href="{_URLS["counts.csv"]}"' in sanitized.html


@pytest.mark.parametrize(
    "href", ["javascript:alert(1)", "data:text/html,boom", "/admin", "data/missing.csv"]
)
def test_a_link_outside_the_web_and_the_attachments_loses_its_target(href: str) -> None:
    sanitized = sanitize_formatted_prompt(f'<a href="{href}">x</a>', attachment_urls=_URLS)
    assert "href=" not in sanitized.html


def test_only_layout_classes_survive() -> None:
    sanitized = sanitize_formatted_prompt(
        '<p class="fp-justify fixed inset-0 z-50">Text</p><div class="bg-red-500">x</div>',
        attachment_urls=_URLS,
    )
    assert '<p class="fp-justify">Text</p>' in sanitized.html
    assert "bg-red-500" not in sanitized.html


def test_a_long_link_text_offers_line_breaks_after_its_separators() -> None:
    """A DOI or URL is one unbreakable word, which leaves a justified line
    with wide gaps unless it can wrap at its separators."""
    sanitized = sanitize_formatted_prompt(
        '<p><a href="https://doi.org/10.1038/nature11949">doi:10.1038/nature11949</a> and '
        '<a href="https://www.ebi.ac.uk/gxa">Expression Atlas</a></p>',
        attachment_urls=_URLS,
    )
    assert ">doi:<wbr>10.<wbr>1038/<wbr>nature11949</a>" in sanitized.html
    assert ">Expression Atlas</a>" in sanitized.html
    assert 'href="https://doi.org/10.1038/nature11949"' in sanitized.html


def test_lists_tables_and_headings_are_kept() -> None:
    fragment = (
        '<h2>Aims</h2><ol start="2"><li>One</li></ol><ul><li>Two</li></ul>'
        '<table><thead><tr><th>A</th></tr></thead><tbody><tr><td colspan="2">1</td>'
        "</tr></tbody></table><blockquote>Quote</blockquote>"
    )
    sanitized = sanitize_formatted_prompt(fragment, attachment_urls=_URLS)
    assert sanitized.html == fragment


@pytest.mark.parametrize("lang", ["it", "en-GB", "zh-Hant-TW"])
def test_a_language_tag_is_kept_for_hyphenation(lang: str) -> None:
    sanitized = sanitize_formatted_prompt(
        f'<div lang="{lang}"><p class="fp-justify">Testo</p></div>', attachment_urls=_URLS
    )
    assert f'<div lang="{lang}">' in sanitized.html


@pytest.mark.parametrize("lang", ["", "english language", "e", "en_US", 'en" onclick="x'])
def test_a_malformed_language_tag_is_dropped(lang: str) -> None:
    sanitized = sanitize_formatted_prompt(f"<p lang='{lang}'>Text</p>", attachment_urls=_URLS)
    assert sanitized.html == "<p>Text</p>"


_FORMATTED = (
    '<h2>Aims</h2><p class="fp-justify">Compare the bands in '
    '<a href="data/counts.csv">the counts</a>.</p>'
    '<figure class="fp-float-right fp-small"><img src="data/fig 1.png" alt="Gel"></figure>'
    "<script>steal()</script>"
)


async def _seed(
    test_engine: AsyncEngine, *, formatted_prompt: str | None, question: str, is_public: bool
) -> dict[str, Any]:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@prompt.test", name="Owner", is_approved=True)
        reader = User(email=f"reader-{uuid4().hex}@prompt.test", name="Reader", is_approved=True)
        db.add_all([owner, reader])
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question=question,
            formatted_prompt=formatted_prompt,
            status="running",
            current_iteration=1,
            max_iterations=5,
            is_public=is_public,
        )
        db.add(job)
        await db.flush()
        for name, file_type, mime in (
            ("fig 1.png", "image", "image/png"),
            ("blot.jpg", "image", "image/jpeg"),
            ("counts.csv", "csv", "text/csv"),
            ("notes.pdf", "pdf", "application/pdf"),
        ):
            db.add(
                JobDataFile(
                    job_id=job.id,
                    filename=name,
                    file_path=f"data/{name}",
                    file_type=file_type,
                    file_size=2048,
                    mime_type=mime,
                )
            )
        owner_login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        reader_login = Session(user_id=reader.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add_all([owner_login, reader_login])
        await db.commit()
    return {
        "job_id": str(job.id),
        "owner_token": str(owner_login.id),
        "reader_token": str(reader_login.id),
        "users": [owner.id, reader.id],
    }


async def _cleanup(test_engine: AsyncEngine, seeded: dict[str, Any]) -> None:
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == UUID(seeded["job_id"])))
        await db.execute(delete(User).where(User.id.in_(seeded["users"])))
        await db.commit()


@pytest_asyncio.fixture
async def formatted_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    seeded = await _seed(
        test_engine,
        formatted_prompt=_FORMATTED,
        question="Aims: compare the bands in counts.csv, see fig 1.png",
        is_public=True,
    )
    yield seeded
    await _cleanup(test_engine, seeded)


@pytest_asyncio.fixture
async def unformatted_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    seeded = await _seed(
        test_engine, formatted_prompt=None, question="Does X bind Y?", is_public=False
    )
    yield seeded
    await _cleanup(test_engine, seeded)


@pytest_asyncio.fixture
async def long_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    paragraphs = "".join(f"<p>Paragraph {i}: {'word ' * 60}</p>" for i in range(12))
    seeded = await _seed(
        test_engine,
        formatted_prompt=f"<h2>Plan</h2>{paragraphs}",
        question="A long question",
        is_public=False,
    )
    yield seeded
    await _cleanup(test_engine, seeded)


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The page reads the job directory and the latest job from the job manager."""

    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=JobStatus.RUNNING)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )


async def _build(served_page: ServedPage, token: str, job_id: str) -> Client:
    client, _ = await served_page(token, lambda: job_detail_page(job_id), lambda c: None)
    return client


def _elements[T](client: Client, kind: type[T]) -> list[T]:
    with client:
        return [e for e in client.elements.values() if isinstance(e, kind)]


def _prompt_html(client: Client) -> ui.html | None:
    return next((e for e in _elements(client, ui.html) if "os-prompt" in e.classes), None)


def _original(client: Client) -> ui.label:
    return next(e for e in _elements(client, ui.label) if "os-prompt-original" in e.classes)


def _button(client: Client, text: str) -> ui.button | None:
    return next((e for e in _elements(client, ui.button) if e.text == text), None)


def _body(client: Client) -> ui.element:
    with client:
        return next(e for e in client.elements.values() if "os-prompt-body" in e.classes)


def _gallery(client: Client) -> str:
    gallery = next(
        (e for e in _elements(client, ui.html) if "os-prompt-gallery" in e.classes), None
    )
    return gallery.content if gallery is not None else ""


def _attachments(client: Client) -> dict[str, tuple[str, list[str]]]:
    """The listed attachments by name, with their link and icon classes."""
    listed: dict[str, tuple[str, list[str]]] = {}
    for link in _elements(client, ui.link):
        if "os-attachment" not in link.classes:
            continue
        name = next(d.text for d in link.descendants() if isinstance(d, ui.label))
        icon = next(d for d in link.descendants() if d.tag == "i")
        listed[name] = (link.props["href"], list(icon.classes))
    return listed


def _click(client: Client, button: ui.button) -> None:
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    with client, button.parent_slot or nullcontext():
        listener.handler(None)


@pytest.mark.asyncio
async def test_the_owner_sees_the_sanitized_formatted_prompt(
    database: Forwarder, formatted_job: dict[str, Any], served_page: ServedPage
) -> None:
    job_id = formatted_job["job_id"]
    client = await _build(served_page, formatted_job["owner_token"], job_id)

    html = _prompt_html(client)
    assert html is not None
    assert "<h2>Aims</h2>" in html.content
    assert f'src="{attachment_url(job_id, "data/fig 1.png")}"' in html.content
    assert f'href="{attachment_url(job_id, "data/counts.csv")}"' in html.content
    assert "steal" not in html.content
    assert html.props["lang"] == "en", "justified text hyphenates only in a declared language"
    original = _original(client)
    assert original.text == "Aims: compare the bands in counts.csv, see fig 1.png"
    assert not original.visible


@pytest.mark.asyncio
async def test_show_original_prompt_toggles_the_raw_text(
    database: Forwarder, formatted_job: dict[str, Any], served_page: ServedPage
) -> None:
    client = await _build(served_page, formatted_job["owner_token"], formatted_job["job_id"])

    show = _button(client, "Show original prompt")
    assert show is not None
    _click(client, show)
    assert _original(client).visible
    assert show.text == "Hide original prompt"
    _click(client, show)
    assert not _original(client).visible
    assert show.text == "Show original prompt"


@pytest.mark.asyncio
async def test_unplaced_images_follow_the_prompt_and_other_files_are_listed_with_icons(
    database: Forwarder, formatted_job: dict[str, Any], served_page: ServedPage
) -> None:
    job_id = formatted_job["job_id"]
    client = await _build(served_page, formatted_job["owner_token"], job_id)

    gallery = _gallery(client)
    assert attachment_url(job_id, "data/blot.jpg") in gallery
    assert attachment_url(job_id, "data/fig 1.png") not in gallery
    assert _attachments(client) == {
        "counts.csv": (
            attachment_url(job_id, "data/counts.csv"),
            ["fa-solid", "fa-file-csv"],
        ),
        "notes.pdf": (
            attachment_url(job_id, "data/notes.pdf"),
            ["fa-solid", "fa-file-pdf"],
        ),
    }


@pytest.mark.asyncio
async def test_a_public_reader_gets_neither_images_nor_attachments(
    database: Forwarder, formatted_job: dict[str, Any], served_page: ServedPage
) -> None:
    client = await _build(served_page, formatted_job["reader_token"], formatted_job["job_id"])

    html = _prompt_html(client)
    assert html is not None
    assert "<h2>Aims</h2>" in html.content
    assert "/web/jobs/" not in html.content
    assert _gallery(client) == ""
    assert _attachments(client) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "formatted"),
    [
        ("Does X bind Y?", "<p>Does <strong>X</strong> bind <em>Y</em>?</p>"),
        (
            "Aims\n\nCompare A &  B  in   counts.csv",
            '<div lang="en"><h2>Aims</h2><p class="fp-justify">Compare A &amp; B in '
            '<a href="data/counts.csv">counts.csv</a></p></div>',
        ),
    ],
)
async def test_a_formatted_prompt_with_the_original_words_offers_no_original(
    database: Forwarder,
    test_engine: AsyncEngine,
    served_page: ServedPage,
    question: str,
    formatted: str,
) -> None:
    """Markup, links and line breaks alone leave nothing the original would
    add, so the page offers no toggle for it."""
    seeded = await _seed(
        test_engine, formatted_prompt=formatted, question=question, is_public=False
    )
    try:
        client = await _build(served_page, seeded["owner_token"], seeded["job_id"])
        assert _prompt_html(client) is not None
        assert _button(client, "Show original prompt") is None
        assert not any("os-prompt-original" in e.classes for e in _elements(client, ui.label))
    finally:
        await _cleanup(test_engine, seeded)


@pytest.mark.asyncio
async def test_without_a_formatted_prompt_the_raw_text_is_the_prompt(
    database: Forwarder, unformatted_job: dict[str, Any], served_page: ServedPage
) -> None:
    job_id = unformatted_job["job_id"]
    client = await _build(served_page, unformatted_job["owner_token"], job_id)

    assert _prompt_html(client) is None
    original = _original(client)
    assert original.text == "Does X bind Y?"
    assert original.visible
    assert _button(client, "Show original prompt") is None
    assert attachment_url(job_id, "data/fig 1.png") in _gallery(client)
    assert attachment_url(job_id, "data/blot.jpg") in _gallery(client)
    assert set(_attachments(client)) == {"counts.csv", "notes.pdf"}


@pytest.mark.asyncio
async def test_a_short_prompt_has_no_show_more(
    database: Forwarder, unformatted_job: dict[str, Any], served_page: ServedPage
) -> None:
    client = await _build(served_page, unformatted_job["owner_token"], unformatted_job["job_id"])

    assert _button(client, "Show more") is None
    assert "os-prompt-collapsed" not in _body(client).classes


@pytest.mark.asyncio
async def test_a_long_prompt_is_collapsed_behind_show_more(
    database: Forwarder, long_job: dict[str, Any], served_page: ServedPage
) -> None:
    client = await _build(served_page, long_job["owner_token"], long_job["job_id"])

    assert "os-prompt-collapsed" in _body(client).classes
    more = _button(client, "Show more")
    assert more is not None
    _click(client, more)
    assert "os-prompt-collapsed" not in _body(client).classes
    assert more.text == "Show less"
    _click(client, more)
    assert "os-prompt-collapsed" in _body(client).classes
    assert more.text == "Show more"


@pytest.mark.asyncio
async def test_a_formatted_prompt_written_while_running_appears_on_the_next_tick(
    database: Forwarder,
    unformatted_job: dict[str, Any],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    job_id = unformatted_job["job_id"]
    client = await _build(served_page, unformatted_job["owner_token"], job_id)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    with client:
        timer = next(
            e for e in client.elements.values() if isinstance(e, ui.timer) and e.interval == 2.0
        )
    assert _prompt_html(client) is None
    assert _refining(client), "a running job without a formatted prompt shows it is refining"

    async with AsyncSession(test_engine) as db:
        job = await db.get(Job, UUID(job_id))
        assert job is not None
        job.formatted_prompt = "<h2>Question</h2><p>Does <em>X</em> bind <strong>Y</strong>?</p>"
        await db.commit()
    assert timer.callback is not None
    with client:
        await timer.callback()
    await drain_outbox(client)

    html = _prompt_html(client)
    assert html is not None
    assert "<em>X</em>" in html.content
    assert not _original(client).visible
    assert _button(client, "Show original prompt") is not None
    assert not _refining(client), "the refining status stays after the prompt arrived"


def _refining(client: Client) -> bool:
    return any(e.text == _REFINING for e in _elements(client, ui.label))


_REFINING = "Refining the research question..."


@pytest.mark.asyncio
async def test_a_job_whose_discovery_started_shows_no_refining_status(
    database: Forwarder,
    unformatted_job: dict[str, Any],
    served_page: ServedPage,
    test_engine: AsyncEngine,
) -> None:
    """The formatting turn runs before the first analysis, so once one is
    logged the turn is over, recorded or not."""
    async with AsyncSession(test_engine) as db:
        db.add(
            AnalysisLog(
                job_id=UUID(unformatted_job["job_id"]),
                iteration=1,
                step_number=1,
                action_type="execute_code",
                description="Run 1",
                input_data={"code": "print(1)"},
            )
        )
        await db.commit()
    client = await _build(served_page, unformatted_job["owner_token"], unformatted_job["job_id"])
    assert not _refining(client)


def _prompt_boxes(fragment: str) -> list[Any]:
    from openscientist.webapp_components.pages.job_detail.prompt import _PROMPT_CSS
    from tests.webapp.css_layout import layout

    return layout(f'<div id="prompt" class="os-prompt">{fragment}</div>', _PROMPT_CSS)


@pytest.mark.requires_weasyprint
def test_the_first_heading_starts_the_prompt_even_inside_its_language_wrapper() -> None:
    from tests.webapp.css_layout import box_by_id

    boxes = _prompt_boxes('<div lang="en"><h2 id="first">Background</h2><p>Text</p></div>')
    assert box_by_id(boxes, "first").position_y == pytest.approx(
        box_by_id(boxes, "prompt").position_y
    )


@pytest.mark.requires_weasyprint
def test_prompt_headings_stay_close_to_the_text_size() -> None:
    """The card's own label is small, so a heading may only step a little above
    the prompt's text, with modest space around it."""
    from tests.webapp.css_layout import box_by_id

    boxes = _prompt_boxes(
        '<p id="text">Text</p><h1 id="h1">One</h1><h2 id="h2">Two</h2><h3 id="h3">Three</h3>'
    )
    text = float(box_by_id(boxes, "text").style["font_size"])
    for heading in ("h1", "h2", "h3"):
        box = box_by_id(boxes, heading)
        assert float(box.style["font_size"]) <= text * 1.05
        assert float(box.margin_top) <= 12
