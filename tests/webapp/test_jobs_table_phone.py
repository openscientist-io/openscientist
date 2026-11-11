"""The /jobs table on a phone: the question wraps and clamps instead of
running past the screen edge, the Job ID and stat columns drop out under
640px, and the row carries the full question so the cell's tooltip can show
it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Job, Session, User
from openscientist.settings import clear_settings_cache
from openscientist.webapp_components.pages.jobs_list import (
    _JOBS_TABLE_STYLES,
    _OWNED_COLUMNS,
    jobs_page,
)
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage

#: Long enough to outgrow the old 50-character truncation and one table line.
_LONG_QUESTION = (
    "Does carnosine depletion drive the thermal tolerance of Drosophila "
    "melanogaster under chronic heat stress in the context of metabolic "
    "reprogramming of the flight muscle?"
)

#: The page's horizontal padding at every viewport (content 16px plus tab
#: panel 16px, each side).
_CONTENT_PAD = 32


class _OwnerRow(TypedDict):
    owned: set[str]
    owner_token: str


@pytest_asyncio.fixture
async def long_job_owner(test_engine: AsyncEngine) -> AsyncIterator[_OwnerRow]:
    """An approved user who owns two long-question jobs and holds a live
    session, all committed."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"phone-{uuid4().hex}@jobs.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        first = Job(
            owner_id=owner.id,
            research_question=_LONG_QUESTION,
            status="completed",
            current_iteration=10,
            max_iterations=10,
        )
        second = Job(
            owner_id=owner.id,
            research_question="How does the gut microbiome mediate lipid storage?",
            status="running",
            current_iteration=2,
            max_iterations=15,
        )
        db.add_all([first, second])
        await db.flush()
        login, login_token = Session.issue(
            user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1)
        )
        db.add(login)
        await db.commit()
    yield {"owned": {str(first.id), str(second.id)}, "owner_token": login_token}
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id.in_([first.id, second.id])))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page hands the job manager only to its delete dialog."""
    monkeypatch.setattr(web_app._state, "job_manager", object())


@pytest.fixture
def configured_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An Anthropic key, so the page offers starting a job."""
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    clear_settings_cache()
    yield
    clear_settings_cache()


def _owned_table(client: Client) -> ui.table:
    tables = [e for e in client.elements.values() if isinstance(e, ui.table)]
    return next(t for t in tables if not any(c["name"] == "owner" for c in t.columns))


def _first_row(table: ui.table) -> dict:
    return table.rows[0]


@pytest.mark.asyncio
async def test_the_rows_carry_the_full_research_question(
    database: Forwarder,
    long_job_owner: _OwnerRow,
    configured_provider: None,
    served_page: ServedPage,
) -> None:
    """The question cell clamps its text and its tooltip shows the full
    question, so the row must carry the question uncut."""
    client, _ = await served_page(long_job_owner["owner_token"], jobs_page, lambda client: None)
    rows = _owned_table(client).rows
    assert rows
    assert _first_row(_owned_table(client))["question"] == _LONG_QUESTION


@pytest.mark.requires_weasyprint
@pytest.mark.parametrize("page_width", [390, 1440])
def test_the_table_fits_the_viewport_and_the_question_wraps(page_width: int) -> None:
    """At a phone viewport the hidden columns drop out and the wrapped
    question keeps the table inside the content width; at 1440 every column
    stays and the table still fits."""
    from tests.webapp.css_layout import box_by_id, layout

    fragment = _table_fragment(_OWNED_COLUMNS, {"question": _LONG_QUESTION, "status": "completed"})
    shim = (
        "<style>"
        ".w-full { width: 100%; }"
        f".nicegui-content {{ box-sizing: border-box; padding: {_CONTENT_PAD}px; width: 100%; }}"
        # The engine only matches the print medium, so at the phone width the
        # hiding the page's media query applies there is stated outright.
        + (".os-jobs-table .os-hide-phone { display: none; }" if page_width <= 640 else "")
        + "</style>"
    )
    boxes = layout(fragment, _JOBS_TABLE_STYLES, shim, quasar=True, page_width=page_width)
    table_box = box_by_id(boxes, "table")
    question_cell = box_by_id(boxes, "question-cell")
    container = box_by_id(boxes, "content")

    content_width = page_width - 2 * _CONTENT_PAD
    assert float(container.width) == pytest.approx(content_width, abs=1)
    assert float(table_box.width) <= content_width, "the table stays inside the screen"
    assert float(question_cell.position_x) + float(question_cell.width) <= (
        float(table_box.position_x) + float(table_box.width)
    ), "no column runs past the table's edge"
    headers = {
        box.element.get("data-label")
        for box in boxes
        if box.element is not None and getattr(box, "element_tag", None) == "th"
    }
    if page_width == 390:
        assert len(_cell_text_lines(question_cell)) >= 2, "the question wraps to a few lines"
        hidden_labels = {
            c["label"] for c in _OWNED_COLUMNS if "os-hide-phone" in (c.get("headerClasses") or "")
        }
        assert not hidden_labels & headers, "the hidden columns are gone at a phone width"
    else:
        assert headers == {c["label"] for c in _OWNED_COLUMNS}, "every column stays at 1440"


@pytest.mark.requires_weasyprint
def test_the_question_cell_justifies_with_hyphenation() -> None:
    """A narrow phone cell left ragged shows a wide empty band, so the
    question's lines fill the cell by justifying and breaking long words by
    hyphenation."""
    from tests.webapp.css_layout import box_by_id, layout

    fragment = _table_fragment(_OWNED_COLUMNS, {"question": _LONG_QUESTION, "status": "completed"})
    shim = (
        "<style>"
        ".w-full { width: 100%; }"
        f".nicegui-content {{ box-sizing: border-box; padding: {_CONTENT_PAD}px; width: 100%; }}"
        ".os-jobs-table .os-hide-phone { display: none; }"
        "</style>"
    )
    boxes = layout(fragment, _JOBS_TABLE_STYLES, shim, quasar=True, page_width=390)
    span = box_by_id(boxes, "question-span")
    assert span.style["text_align_all"] == "justify"
    assert span.style["hyphens"] == "auto"


def _cell_text_lines(box: object) -> list[float]:
    """The heights of the text lines inside the cell. The engine sets no
    parent links on the box tree, so walk downward from the cell."""
    lines: list[float] = []

    def rec(node: object) -> None:
        for child in getattr(node, "children", []) or []:
            if type(child).__name__ == "LineBox":
                lines.append(float(getattr(child, "height", 0.0)))
            rec(child)

    rec(box)
    return lines


def _table_fragment(columns: list[dict[str, str]], row: dict[str, str]) -> str:
    """The owned jobs table the way the browser lays it out, one row with the
    real question, so the layout engine sees the page's real column classes
    and cell content."""
    ths = "".join(
        f'<th data-label="{c["label"]}" class="{c.get("classes", "")}">{c["label"]}</th>'
        for c in columns
    )
    tds = "".join(_cell(c, row) for c in columns)
    return (
        '<div id="content" class="nicegui-content">'
        '<div class="q-table__container q-table__card q-table--no-wrap w-full os-jobs-table">'
        '<div class="q-table__middle">'
        f'<table id="table" class="q-table"><thead><tr>{ths}</tr></thead>'
        f"<tbody><tr>{tds}</tr></tbody></table>"
        "</div></div></div>"
    )


def _cell(column: dict[str, str], row: dict[str, str]) -> str:
    """One body cell with the content the page's slot templates render."""
    name = column["name"]
    cls = column.get("classes", "")
    extra = ' id="question-cell"' if name == "question" else ""
    if name == "job_id":
        content = '<span style="white-space:nowrap;display:inline-block;width:90px">abcd1234</span>'
    elif name == "question":
        content = f'<span id="question-span" class="os-jobs-question">{row["question"]}</span>'
    elif name == "status":
        content = f'<span style="white-space:nowrap">✓ {row["status"]}</span>'
    elif name == "actions":
        content = (
            '<span style="display:inline-flex;gap:4px">'
            '<span style="display:inline-block;width:24px;height:24px"></span>'
            '<span style="display:inline-block;width:24px;height:24px"></span>'
            "</span>"
        )
    else:
        content = {
            "iterations": "2/15",
            "findings": "0",
            "cost": "$0.42",
            "created": "2026-11-08",
        }.get(name, row.get(name, ""))
    return f'<td class="{cls}"{extra}>{content}</td>'
