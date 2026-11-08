"""The research question card of the job page: the action buttons share the
card's title line, and the question body below takes the card's full width at
every viewport, so a phone gets no empty band down the right side."""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
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
from openscientist.database.models import Job, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from openscientist.webapp_components.pages.job_detail.prompt import _PROMPT_CSS
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage

_QUESTION = (
    "Determine whether carnosine depletion drives the thermal tolerance of "
    "Drosophila melanogaster under chronic heat stress in the context of "
    "metabolic reprogramming of the flight muscle, distinguishing muscle "
    "autonomy from whole-organism feedback. Work through the primary "
    "literature on the heat-shock response, dipeptide transport and "
    "flight-muscle metabolism, then test the depletion with a genetic "
    "knockdown in an isogenic background and compare the thermal limits "
    "against a matched control line."
)

#: The utilities the card's rows and columns use that the layout engine does
#: not generate in the browser.
_TAILWIND_SHIM = """
<style>
.w-full { width: 100%; }
.mb-4 { margin-bottom: 16px; }
.mt-1 { margin-top: 4px; }
.mt-3 { margin-top: 12px; }
.p-3 { padding: 12px; }
.gap-1 { gap: 4px; }
.gap-2 { gap: 8px; }
.items-center { align-items: center; }
.justify-between { justify-content: space-between; }
.nicegui-row, .row { display: flex; flex-wrap: wrap; }
.nicegui-column { display: flex; flex-direction: column; }
.flex-1 { flex: 1 1 0%; }
.min-w-0 { min-width: 0; }
.font-bold { font-weight: 700; }
.text-subtitle2 { font-size: 1.25rem; }
.text-sm { font-size: 0.875rem; }
.text-xs { font-size: 0.75rem; }
.self-start { align-self: flex-start; }
.border-l-4 { border-left: 4px solid #10b981; }
.rounded { border-radius: 4px; }
.os-prompt-collapsed { max-height: 12rem; overflow: hidden; }
</style>
"""

#: A round dense action button the way the browser sizes it.
_BUTTON_PLACEHOLDER = '<div style="width:24px;height:24px;display:inline-block"></div>'


async def _seed(test_engine: AsyncEngine) -> dict[str, str]:
    """An owner and a completed job whose question and consensus answer make
    the card show its full content."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"card-{uuid4().hex}@card.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question=_QUESTION,
            status="completed",
            current_iteration=10,
            max_iterations=10,
            consensus_answer="Carnosine depletion lowers the upper critical temperature.",
        )
        db.add(job)
        await db.flush()
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    return {"job_id": str(job.id), "owner_token": str(login.id), "user": str(owner.id)}


async def _cleanup(test_engine: AsyncEngine, seeded: dict[str, str]) -> None:
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == UUID(seeded["job_id"])))
        await db.execute(delete(User).where(User.id == UUID(seeded["user"])))
        await db.commit()


@pytest_asyncio.fixture
async def question_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    seeded = await _seed(test_engine)
    yield seeded
    await _cleanup(test_engine, seeded)


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The page reads the job directory and the latest job from the manager."""

    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=JobStatus.COMPLETED)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )


async def _build(served_page: ServedPage, token: str, job_id: str) -> Client:
    client, _ = await served_page(token, lambda: job_detail_page(job_id), lambda c: None)
    return client


def _card(client: Client) -> ui.card:
    """The research question card, the one whose descendants hold its label."""
    with client:
        for element in client.elements.values():
            if not isinstance(element, ui.card):
                continue
            labels = [d.text for d in element.descendants() if isinstance(d, ui.label)]
            if "Research Question" in labels:
                return element
    raise AssertionError("no research question card on the page")


def _title_row(card: ui.card) -> ui.row:
    row = card.default_slot.children[0]
    assert isinstance(row, ui.row)
    return row


def _prompt_body(client: Client) -> ui.element:
    with client:
        return next(e for e in client.elements.values() if "os-prompt-body" in e.classes)


def _label_of(row: ui.row) -> ui.label:
    return next(c for c in row.default_slot.children if isinstance(c, ui.label))


def _buttons_row(row: ui.row) -> ui.row:
    buttons = next(c for c in row.default_slot.children if isinstance(c, ui.row))
    assert isinstance(buttons, ui.row)
    return buttons


def _parent(client: Client, element: ui.element) -> ui.element:
    with client:
        for el in client.elements.values():
            if element in el.default_slot.children:
                return el
    raise AssertionError(f"no element holds {element}")


@pytest.mark.asyncio
async def test_the_consensus_block_sits_in_the_card_below_the_prompt(
    database: Forwarder, question_job: dict[str, str], served_page: ServedPage
) -> None:
    """A completed job's consensus answer stays inside the card, out of the
    title line."""
    client = await _build(served_page, question_job["owner_token"], question_job["job_id"])
    card = _card(client)
    title = _title_row(card)

    consensus_labels = [
        d for d in card.descendants() if isinstance(d, ui.label) and d.text == "Consensus Answer"
    ]
    assert len(consensus_labels) == 1
    assert consensus_labels[0] not in title.default_slot.children
    body = _prompt_body(client)
    consensus_column = _parent(client, consensus_labels[0])
    assert body not in consensus_column.default_slot.children


@pytest.mark.asyncio
@pytest.mark.requires_weasyprint
@pytest.mark.parametrize("page_width", [390, 1440])
async def test_the_prompt_body_spans_the_card_at_every_width(
    database: Forwarder,
    question_job: dict[str, str],
    served_page: ServedPage,
    page_width: int,
) -> None:
    """The title line holds the label and the buttons, and the body below
    takes the card's full content width at a phone and a desktop viewport."""
    from tests.webapp.css_layout import box_by_id, layout

    client = await _build(served_page, question_job["owner_token"], question_job["job_id"])
    card = _card(client)
    title = _title_row(card)
    label = _label_of(title)
    buttons = _buttons_row(title)

    prompt_label = _prompt_label(client)
    fragment = (
        f'<div id="card" class="{" ".join(card.classes)}">'
        f'<div id="title" class="{" ".join(title.classes)}">'
        f'<div id="label" class="{" ".join(label.classes)}">Research Question</div>'
        f'<div id="buttons" class="{" ".join(buttons.classes)}">'
        + "".join(
            _BUTTON_PLACEHOLDER for c in buttons.default_slot.children if isinstance(c, ui.button)
        )
        + "</div></div>"
        + _prompt_block(client, prompt_label)
        + "</div>"
    )
    boxes = layout(fragment, _PROMPT_CSS, _TAILWIND_SHIM, quasar=True, page_width=page_width)
    title_box = box_by_id(boxes, "title")
    buttons_box = box_by_id(boxes, "buttons")
    body_box = box_by_id(boxes, "body")

    assert float(body_box.width) == pytest.approx(float(title_box.width), abs=1), (
        "the body takes the card's full content width"
    )
    assert (
        float(buttons_box.position_y) >= float(title_box.position_y) - 1
        and float(buttons_box.position_y) + float(buttons_box.height)
        <= float(title_box.position_y) + float(title_box.height) + 1
    ), "the buttons sit on the title line, not in a row of their own"
    assert (
        float(body_box.position_y) >= float(title_box.position_y) + float(title_box.height) - 1
    ), "the body starts below the title line"


def _prompt_label(client: Client) -> ui.label:
    return next(
        e
        for e in client.elements.values()
        if isinstance(e, ui.label) and "os-prompt-question" in e.classes
    )


def _prompt_block(client: Client, question: ui.label) -> str:
    """The question body as the browser lays it out, so the layout engine sees
    the card's real classes and content."""
    with client:
        body = next(e for e in client.elements.values() if "os-prompt-body" in e.classes)
        column = _parent(client, body)
        parts = [
            f'<div id="body" class="{" ".join(body.classes)}">'
            f'<div class="{" ".join(question.classes)}" lang="en">'
            f"{html.escape(question.text)}</div></div>"
        ]
        parts.append("</div>")
    return f'<div class="{" ".join(column.classes)}">' + "".join(parts)
