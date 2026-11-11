"""The research question card of the job page: the question as the scientist
wrote it, the attached images in a gallery below it, the other attachments
listed under it and a Show more for a long question."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, JobDataFile, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from openscientist.webapp_components.pages.job_detail.prompt import attachment_url
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage

_JOB = "0190b6c4-0000-7000-8000-000000000001"


def test_attachment_url_quotes_the_path() -> None:
    assert attachment_url(_JOB, "data/fig 1.png") == f"/web/jobs/{_JOB}/files/data/fig%201.png"


_QUESTION = "Aims: compare the bands in counts.csv, see fig 1.png"


async def _seed(
    test_engine: AsyncEngine,
    *,
    question: str,
    is_public: bool,
    agent_status: str | None = None,
) -> tuple[dict[str, str], tuple[UUID, UUID]]:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@prompt.test", name="Owner", is_approved=True)
        reader = User(email=f"reader-{uuid4().hex}@prompt.test", name="Reader", is_approved=True)
        db.add_all([owner, reader])
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question=question,
            status="running",
            current_iteration=1,
            max_iterations=5,
            is_public=is_public,
            agent_status=agent_status,
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
        owner_login, owner_login_token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        reader_login, reader_login_token = Session.issue(
            user_id=reader.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add_all([owner_login, reader_login])
        await db.commit()
    return (
        {
            "job_id": str(job.id),
            "owner_token": owner_login_token,
            "reader_token": reader_login_token,
        },
        (owner.id, reader.id),
    )


async def _cleanup(
    test_engine: AsyncEngine, seeded: dict[str, str], users: tuple[UUID, UUID]
) -> None:
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == UUID(seeded["job_id"])))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()


@pytest_asyncio.fixture
async def question_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    seeded, users = await _seed(test_engine, question=_QUESTION, is_public=True)
    yield seeded
    await _cleanup(test_engine, seeded, users)


@pytest_asyncio.fixture
async def short_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    seeded, users = await _seed(
        test_engine, question="Does X bind Y?", is_public=False, agent_status="Thinking"
    )
    yield seeded
    await _cleanup(test_engine, seeded, users)


@pytest_asyncio.fixture
async def long_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    paragraphs = " ".join(f"Paragraph {i} " + "word " * 60 for i in range(12))
    seeded, users = await _seed(
        test_engine, question=f"A long question. {paragraphs}", is_public=False
    )
    yield seeded
    await _cleanup(test_engine, seeded, users)


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


def _question_label(client: Client) -> ui.label:
    return next(e for e in _elements(client, ui.label) if "os-prompt-question" in e.classes)


def _button(client: Client, text: str) -> ui.button | None:
    return next((e for e in _elements(client, ui.button) if e.text == text), None)


def _body(client: Client) -> ui.element:
    with client:
        return next(e for e in client.elements.values() if "os-prompt-body" in e.classes)


def _click(client: Client, button: ui.button) -> None:
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    with client, button.parent_slot or nullcontext():
        listener.handler(None)


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


@pytest.mark.asyncio
async def test_the_owner_sees_the_question_as_written(
    database: Forwarder, question_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _build(served_page, question_job["owner_token"], question_job["job_id"])

    question = _question_label(client)
    assert question.text == _QUESTION
    assert question.props["lang"] == "en", "justified text hyphenates only in a declared language"


@pytest.mark.asyncio
async def test_attached_images_go_to_the_gallery_and_other_files_to_chips(
    database: Forwarder, question_job: dict[str, str], served_page: ServedPage
) -> None:
    job_id = question_job["job_id"]
    client = await _build(served_page, question_job["owner_token"], job_id)

    gallery = _gallery(client)
    assert attachment_url(job_id, "data/fig 1.png") in gallery
    assert attachment_url(job_id, "data/blot.jpg") in gallery
    assert attachment_url(job_id, "data/counts.csv") not in gallery
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
    database: Forwarder, question_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _build(served_page, question_job["reader_token"], question_job["job_id"])

    assert _question_label(client).text == _QUESTION
    assert "/web/jobs/" not in _question_label(client).text
    assert _gallery(client) == ""
    assert _attachments(client) == {}


@pytest.mark.asyncio
async def test_a_short_question_has_no_show_more(
    database: Forwarder, short_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _build(served_page, short_job["owner_token"], short_job["job_id"])

    assert _button(client, "Show more") is None
    assert "os-prompt-collapsed" not in _body(client).classes


@pytest.mark.asyncio
async def test_a_long_question_is_collapsed_behind_show_more(
    database: Forwarder, long_job: dict[str, str], served_page: ServedPage
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


@pytest.mark.requires_weasyprint
def test_the_question_is_justified_and_hyphenated() -> None:
    """Ragged lines leave a wide empty band down a narrow phone card, so the
    question's text fills its lines and breaks long words."""
    from openscientist.webapp_components.pages.job_detail.prompt import _PROMPT_CSS
    from tests.webapp.css_layout import box_by_id, layout

    boxes = layout(
        '<div id="prompt" class="os-prompt-body">'
        '<div id="para" class="os-prompt-question" lang="en">Text</div></div>',
        _PROMPT_CSS,
        quasar=True,
    )
    style = box_by_id(boxes, "para").style
    assert style["text_align_all"] == "justify"
    assert style["hyphens"] == "auto"


def _image_data_uri(width: int, height: int) -> str:
    import base64
    import io

    from PIL import Image

    image = Image.new("RGB", (width, height), (190, 18, 60))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.mark.requires_weasyprint
def test_a_tall_gallery_image_is_capped_without_distorting_its_ratio() -> None:
    """A portrait figure fits the height cap by shrinking to keep its ratio,
    not by stretching a full width at a clamped height."""
    from openscientist.webapp_components.pages.job_detail.prompt import _PROMPT_CSS
    from tests.webapp.css_layout import box_by_id, layout

    fragment = (
        '<div id="prompt" class="os-prompt-gallery">'
        f'<img id="tall" src="{_image_data_uri(200, 1000)}" alt="Tall"></div>'
    )
    boxes = layout(fragment, _PROMPT_CSS, quasar=True)
    box = box_by_id(boxes, "tall")
    assert box.height <= 14 * 16
    assert box.width / box.height == pytest.approx(200 / 1000, rel=0.01)
