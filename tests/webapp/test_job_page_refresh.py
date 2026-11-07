"""The job page's 2 s poll against a running job, built in a real signed-in
NiceGUI client. An idle tick runs only the snapshot and sends nothing, and a
changed tick updates the cards in place, keeping an open card open with the
actions it already shows.

The seed is a running job at iteration 3 of 5, agent status Thinking.
Iteration 1 holds a summary and two code runs, iteration 2 a summary and one
code run, and iteration 3 one code run in progress.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, ui
from nicegui.client import Client
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.engine import get_engine
from openscientist.database.models import (
    AnalysisLog,
    Finding,
    Hypothesis,
    IterationSummary,
    Job,
    Literature,
    Session,
    User,
)
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from openscientist.webapp_components.pages.job_detail.common import _page_snapshot
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage, WireMessage, drain_outbox


@pytest_asyncio.fixture
async def running_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A running job at iteration 3 of 5, its committed log and the owner's
    live session."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@refresh.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="What keeps the poll alive",
            status="running",
            current_iteration=3,
            max_iterations=5,
            agent_status="Thinking",
        )
        db.add(job)
        await db.flush()
        start = datetime.now(UTC) - timedelta(days=1)
        db.add_all(
            [
                IterationSummary(
                    job_id=job.id, iteration=1, summary_text="Pass one", strapline="First pass"
                ),
                IterationSummary(
                    job_id=job.id, iteration=2, summary_text="Pass two", strapline="Second pass"
                ),
                AnalysisLog(
                    job_id=job.id,
                    iteration=1,
                    step_number=1,
                    action_type="execute_code",
                    description="Run 1",
                    input_data={"code": "print(1)"},
                    created_at=start,
                ),
                AnalysisLog(
                    job_id=job.id,
                    iteration=1,
                    step_number=2,
                    action_type="execute_code",
                    description="Run 2",
                    input_data={"code": "print(2)"},
                    created_at=start + timedelta(seconds=1),
                ),
                AnalysisLog(
                    job_id=job.id,
                    iteration=2,
                    step_number=1,
                    action_type="execute_code",
                    description="Run 3",
                    input_data={"code": "print(3)"},
                    created_at=start + timedelta(seconds=2),
                ),
                AnalysisLog(
                    job_id=job.id,
                    iteration=3,
                    step_number=1,
                    action_type="execute_code",
                    description="Run 4",
                    input_data={"code": "print(4)"},
                    created_at=start + timedelta(seconds=3),
                ),
            ]
        )
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {
        "job_id": str(job.id),
        "owner_id": str(owner.id),
        "token": str(login.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id == owner.id))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The page reads the job directory and the latest job from the job manager."""

    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=JobStatus.RUNNING)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )


class _Statements:
    """Every statement the app's database engine executes while open."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self._engine = get_engine().sync_engine

    def _record(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        self.statements.append(statement)

    def __enter__(self) -> list[str]:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self.statements

    def __exit__(self, *exc_info: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)


async def _build(served_page: ServedPage, job: dict[str, str]) -> Client:
    client, _ = await served_page(
        job["token"], lambda: job_detail_page(job["job_id"]), lambda c: None
    )
    return client


def _timer(client: Client) -> ui.timer:
    with client:
        return next(
            e for e in client.elements.values() if isinstance(e, ui.timer) and e.interval == 2.0
        )


async def _tick(
    client: Client, timer: ui.timer, wire_recorder: list[WireMessage]
) -> list[WireMessage]:
    """Fire the poll once, the way the live timer would, and return the
    update messages it put on the wire."""
    callback = timer.callback
    assert callback is not None
    wire_recorder.clear()
    with client:
        await callback()
    await drain_outbox(client)
    return [m for m in wire_recorder if m.event == "update" and m.room == client.id]


def _iteration_cards(client: Client) -> list[ui.expansion]:
    """The iteration cards, the only expansions that own a header slot."""
    with client:
        return [
            e
            for e in client.elements.values()
            if isinstance(e, ui.expansion) and "header" in e.slots
        ]


def _card_labels(card: ui.expansion) -> list[str]:
    labels: list[str] = []
    for element in card.slots["header"].children:
        for child in (element, *element.descendants()):
            if isinstance(child, ui.label):
                labels.append(child.text)
    return labels


def _card_number(card: ui.expansion) -> int:
    for text in _card_labels(card):
        if text.startswith("Iteration "):
            return int(text.split()[1].rstrip(":"))
    raise AssertionError("the card header has no iteration label")


def _card(client: Client, number: int) -> ui.expansion:
    for card in _iteration_cards(client):
        if _card_number(card) == number:
            return card
    raise AssertionError(f"no card for iteration {number}")


def _header_text(card: ui.expansion) -> str:
    for text in _card_labels(card):
        if text.startswith("Iteration "):
            return text
    raise AssertionError("the card header has no iteration label")


def _header_badges(card: ui.expansion) -> set[str]:
    badges: set[str] = set()
    for element in card.slots["header"].children:
        for child in (element, *element.descendants()):
            if isinstance(child, ui.badge):
                badges.add(child.text)
    return badges


async def _open_card(client: Client, card: ui.expansion) -> None:
    """Open the card and wait for its body load to finish."""
    with client:
        before = set(background_tasks.running_tasks)
        card.value = True
    opened = background_tasks.running_tasks - before
    assert opened, "opening the card must schedule its body load"
    for task in opened:
        await asyncio.wait_for(task, 10)


def _close_card(client: Client, card: ui.expansion) -> None:
    with client:
        card.value = False


def _actions_expansion(card: ui.expansion) -> ui.expansion | None:
    for e in card.descendants():
        if isinstance(e, ui.expansion) and (e.props.get("label") or "").startswith("Actions ("):
            return e
    return None


def _actions_label(card: ui.expansion) -> str | None:
    actions = _actions_expansion(card)
    return actions.props.get("label") if actions is not None else None


def _shown_actions(card: ui.expansion) -> list[str]:
    """The titles of the action cards the card currently shows."""
    actions = _actions_expansion(card)
    assert actions is not None, "the card has no Actions section"
    return [
        child.text
        for child in actions.descendants()
        if isinstance(child, ui.label) and child.text.startswith("\u2705 ")
    ]


def _show_more(card: ui.expansion) -> ui.button | None:
    for e in card.descendants():
        if isinstance(e, ui.button) and e.text == "Show more":
            return e
    return None


def _section_label(card: ui.expansion, prefix: str) -> str | None:
    """The label of the card's section expansion starting with prefix."""
    for e in card.descendants():
        if isinstance(e, ui.expansion) and (e.props.get("label") or "").startswith(prefix):
            return e.props.get("label")
    return None


def _placeholder_body(card: ui.expansion) -> bool:
    for e in card.descendants():
        if isinstance(e, ui.label) and e.text == "Click to load details...":
            return True
    return False


def _body_text(card: ui.expansion) -> str:
    """The visible texts of the card's body, labels and html content, joined
    so a caller can substring-check them."""
    texts: list[str] = []
    for e in card.descendants():
        if isinstance(e, ui.label):
            texts.append(e.text)
        elif isinstance(e, ui.html):
            texts.append(e.content)
    return " ".join(texts)


_STATS_BADGE = re.compile(r"^(Status|Progress|Findings|Papers|Hypotheses): ")


def _stats_badges(client: Client) -> dict[str, str]:
    """The stats card's badges by name, their 'Name: value' labels."""
    with client:
        badges: dict[str, str] = {}
        for e in client.elements.values():
            if isinstance(e, ui.label):
                m = _STATS_BADGE.match(e.text or "")
                if m:
                    badges[m.group(1)] = e.text.removeprefix(f"{m.group(1)}: ")
        return badges


def _stats_badge_ids(client: Client) -> set[int]:
    """The identities of the stats badge labels, so a test can tell a
    refreshed card from an untouched one."""
    with client:
        return {
            id(e)
            for e in client.elements.values()
            if isinstance(e, ui.label) and _STATS_BADGE.match(e.text or "")
        }


def _status_line(client: Client) -> str | None:
    with client:
        for e in client.elements.values():
            if isinstance(e, ui.label) and "thinking-label" in e.classes:
                text: str = e.text
                return text
    return None


async def _update_job(test_engine: AsyncEngine, job_id: str, **fields: Any) -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        job = await db.get(Job, UUID(job_id))
        assert job is not None
        for name, value in fields.items():
            setattr(job, name, value)
        await db.commit()


async def _add_log_row(
    test_engine: AsyncEngine, job_id: str, *, iteration: int, step: int, description: str
) -> None:
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        db.add(
            AnalysisLog(
                job_id=UUID(job_id),
                iteration=iteration,
                step_number=step,
                action_type="execute_code",
                description=description,
                input_data={"code": f"print('{description}')"},
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_an_idle_tick_runs_only_the_snapshot_and_sends_nothing(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
) -> None:
    """A tick over an unchanged job runs the snapshot and nothing else, and
    puts no update on the wire."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    job_manager = web_app._state.job_manager
    assert job_manager is not None
    with _Statements() as baseline:
        await _page_snapshot(job["job_id"], job["owner_id"], job_manager.jobs_dir / job["job_id"])
    with _Statements() as tick:
        sent = await _tick(client, timer, wire_recorder)
    assert not sent, f"an idle tick put {len(sent)} updates on the wire"
    assert list(tick) == list(baseline), "the idle tick ran more than the snapshot"


@pytest.mark.asyncio
async def test_a_new_log_row_updates_the_cards_in_place_and_keeps_them_open(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """New code runs in iterations 1 and 2 move the affected headers and
    Actions totals in place, offer the new rows through Show more, keep the
    open card open with its shown actions and leave the stats card untouched."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card1 = _card(client, 1)
    await _open_card(client, card1)
    shown1 = _shown_actions(card1)
    assert shown1 == ["\u2705 Run 1", "\u2705 Run 2"]
    assert _actions_label(card1) == "Actions (2)"
    assert _show_more(card1) is None
    card2 = _card(client, 2)
    await _open_card(client, card2)
    _close_card(client, card2)
    stats_ids = _stats_badge_ids(client)
    stats_badges = _stats_badges(client)
    card3 = _card(client, 3)
    assert _placeholder_body(card3)

    await _add_log_row(test_engine, job["job_id"], iteration=1, step=3, description="Run 5")
    await _add_log_row(test_engine, job["job_id"], iteration=2, step=2, description="Run 6")
    await _tick(client, timer, wire_recorder)

    assert card1.value is True, "the tick closed the open card"
    assert _header_text(card1) == "Iteration 1: First pass"
    assert "3 analyses" in _header_badges(card1)
    assert _actions_label(card1) == "Actions (3)"
    assert _show_more(card1) is not None, "the new row must be offered through Show more"
    assert _shown_actions(card1) == shown1, "the shown actions were rebuilt by the tick"

    assert not card2.value
    assert "2 analyses" in _header_badges(card2)
    await _open_card(client, card2)
    assert _shown_actions(card2) == ["\u2705 Run 3", "\u2705 Run 6"]
    assert _actions_label(card2) == "Actions (2)"

    assert _placeholder_body(card3), "the never opened card was rebuilt"
    assert _stats_badges(client) == stats_badges, "the stats card changed for a log-only row"
    assert _stats_badge_ids(client) == stats_ids, "the stats card was refreshed for a log-only row"


@pytest.mark.asyncio
async def test_a_new_hypothesis_refreshes_the_stats_and_rerenders_its_card(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """A new hypothesis of iteration 2 adds the Hypotheses badge, updates the
    closed card's header and marks its body stale, and leaves the open card
    1 open with its shown actions."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card1 = _card(client, 1)
    await _open_card(client, card1)
    shown1 = _shown_actions(card1)
    card2 = _card(client, 2)
    await _open_card(client, card2)
    _close_card(client, card2)
    assert "Hypotheses" not in _stats_badges(client)

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        db.add(
            Hypothesis(
                job_id=UUID(job["job_id"]),
                iteration=2,
                text="The log drives the poll",
                status="pending",
            )
        )
        await db.commit()

    await _tick(client, timer, wire_recorder)

    assert _stats_badges(client).get("Hypotheses") == "1"
    assert card1.value is True, "the tick closed the open card"
    assert _shown_actions(card1) == shown1, "the open card's actions were rebuilt by the tick"
    assert "1 hypotheses" in _header_badges(card2)
    assert _section_label(card2, "Hypotheses") is None
    await _open_card(client, card2)
    assert _section_label(card2, "Hypotheses") == "Hypotheses (1)"
    assert "The log drives the poll" in _body_text(card2)


@pytest.mark.asyncio
async def test_a_new_finding_rerenders_the_open_card_and_marks_its_border(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """A new finding of the open iteration 1 adds the Findings badge, renders
    the Findings section in place and moves the card's border to green."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card1 = _card(client, 1)
    await _open_card(client, card1)
    shown1 = _shown_actions(card1)
    assert "border-blue-300" in card1.classes
    assert _stats_badges(client).get("Findings") == "0"

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        db.add(
            AnalysisLog(
                job_id=UUID(job["job_id"]),
                iteration=1,
                step_number=3,
                action_type="update_knowledge_state",
                description="Log the finding",
                input_data={"title": "The first pass ran"},
                created_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        db.add(
            Finding(
                job_id=UUID(job["job_id"]),
                iteration=1,
                text="The first pass ran",
                finding_type="discovery",
                source="agent",
                data={"evidence": "Run 1 and Run 2 completed"},
            )
        )
        await db.commit()

    await _tick(client, timer, wire_recorder)

    assert _stats_badges(client).get("Findings") == "1"
    assert card1.value is True, "the tick closed the open card"
    assert _shown_actions(card1) == shown1
    assert "border-green-500" in card1.classes
    assert "border-blue-300" not in card1.classes
    assert _section_label(card1, "Findings") == "Findings (1)"
    assert "The first pass ran" in _body_text(card1)


@pytest.mark.asyncio
async def test_new_literature_rerenders_the_open_cards_search_section(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """Two literature rows of iteration 1 behind one search add the Papers
    badge and render the search section of the open card in place."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card1 = _card(client, 1)
    await _open_card(client, card1)
    shown1 = _shown_actions(card1)
    assert _section_label(card1, "Literature searched") is None

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        db.add(
            AnalysisLog(
                job_id=UUID(job["job_id"]),
                iteration=1,
                step_number=3,
                action_type="search_pubmed",
                description="Search one",
                input_data={"query": "query one", "results_count": 2},
                created_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        db.add_all(
            [
                Literature(
                    job_id=UUID(job["job_id"]),
                    iteration=1,
                    title="Paper A",
                    abstract="Abstract A",
                    extra_metadata={"search_query": "query one", "pmid": "1001"},
                ),
                Literature(
                    job_id=UUID(job["job_id"]),
                    iteration=1,
                    title="Paper B",
                    abstract="Abstract B",
                    extra_metadata={"search_query": "query one", "pmid": "1002"},
                ),
            ]
        )
        await db.commit()

    await _tick(client, timer, wire_recorder)

    assert _stats_badges(client).get("Papers") == "2"
    assert card1.value is True
    assert _shown_actions(card1) == shown1
    assert _actions_label(card1) == "Actions (3)", "the search row joins the Actions total"
    assert _section_label(card1, "Literature searched") == "Literature searched (2 papers)"
    assert "Paper A" in _body_text(card1)
    assert "Paper B" in _body_text(card1)


@pytest.mark.asyncio
async def test_a_summary_change_updates_the_headers_and_rerenders_the_summary(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """An edited strapline of iteration 2 and a created summary of iteration
    3 update the headers in place, mark the closed card's body stale, and
    leave the stats card untouched."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card1 = _card(client, 1)
    await _open_card(client, card1)
    shown1 = _shown_actions(card1)
    card2 = _card(client, 2)
    await _open_card(client, card2)
    _close_card(client, card2)
    assert _header_text(card2) == "Iteration 2: Second pass"
    assert _header_text(_card(client, 3)) == "Iteration 3: Investigation in progress..."
    stats_ids = _stats_badge_ids(client)

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        summary2 = (
            await db.execute(
                select(IterationSummary).where(
                    IterationSummary.job_id == UUID(job["job_id"]),
                    IterationSummary.iteration == 2,
                )
            )
        ).scalar_one()
        summary2.strapline = "Second pass, revised"
        summary2.summary_text = "Pass two, revised"
        db.add(
            IterationSummary(
                job_id=UUID(job["job_id"]),
                iteration=3,
                summary_text="Pass three",
                strapline="Third pass",
            )
        )
        await db.commit()

    await _tick(client, timer, wire_recorder)

    assert _header_text(card2) == "Iteration 2: Second pass, revised"
    assert _header_text(_card(client, 3)) == "Iteration 3: Third pass [in progress]"
    assert _placeholder_body(_card(client, 3)), "the never opened card was rebuilt"
    assert card1.value is True
    assert _shown_actions(card1) == shown1
    assert _stats_badge_ids(client) == stats_ids, "the stats card was refreshed for a summary"

    await _open_card(client, card2)
    assert "Pass two, revised" in _body_text(card2)


@pytest.mark.asyncio
async def test_a_new_plot_file_rerenders_the_open_cards_visualizations(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
) -> None:
    """A plot file written for the open iteration 1 renders the Visualizations
    section in place, with no database change and no stats refresh."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card1 = _card(client, 1)
    await _open_card(client, card1)
    shown1 = _shown_actions(card1)
    assert _section_label(card1, "Visualizations") is None
    stats_ids = _stats_badge_ids(client)

    job_manager = web_app._state.job_manager
    assert job_manager is not None
    plot_dir = job_manager.jobs_dir / job["job_id"] / "provenance"
    plot_dir.mkdir(parents=True)
    (plot_dir / "iter_1_plot.png").write_bytes(b"\x89PNG")
    (plot_dir / "iter_1_plot.json").write_text(
        json.dumps({"iteration": 1, "description": "The runs", "code": "import numpy"})
    )

    await _tick(client, timer, wire_recorder)

    assert card1.value is True
    assert _shown_actions(card1) == shown1
    assert _section_label(card1, "Visualizations") == "Visualizations (1)"
    assert "The runs" in _body_text(card1)
    assert _stats_badge_ids(client) == stats_ids, "the stats card was refreshed for a plot"


@pytest.mark.asyncio
async def test_an_iteration_advance_updates_the_badge_and_appends_the_new_card(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """Advancing current_iteration to 4 moves the Progress badge, strips the
    in-progress flag from card 3, and appends card 4 closed once it has a
    log row, with the open card 1 left open throughout."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card1 = _card(client, 1)
    await _open_card(client, card1)
    assert _stats_badges(client).get("Progress") == "2/5"
    assert _header_text(_card(client, 3)) == "Iteration 3: Investigation in progress..."

    await _update_job(test_engine, job["job_id"], current_iteration=4)
    await _tick(client, timer, wire_recorder)

    assert _stats_badges(client).get("Progress") == "3/5"
    assert _header_text(_card(client, 3)) == "Iteration 3: Activity logged, but no summary recorded"
    assert len(_iteration_cards(client)) == 3, "card 4 appeared before it had activity"
    assert card1.value is True

    await _add_log_row(test_engine, job["job_id"], iteration=4, step=1, description="Run 7")
    await _tick(client, timer, wire_recorder)

    card4 = _card(client, 4)
    assert not card4.value, "the new card must appear closed"
    assert "1 analyses" in _header_badges(card4)
    assert _header_text(card4) == "Iteration 4: Investigation in progress..."
    assert card1.value is True


@pytest.mark.asyncio
async def test_a_completed_status_stops_the_poll_and_reloads_the_page(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move to completed stops the poll and navigates to the job, where the
    page is rebuilt for the finished state."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    navigated: list[str] = []
    monkeypatch.setattr(ui.navigate, "to", lambda url: navigated.append(str(url)))

    await _update_job(test_engine, job["job_id"], status="completed")
    await _tick(client, timer, wire_recorder)

    assert navigated == [f"/job/{job['job_id']}"]
    assert timer.active is False


@pytest.mark.asyncio
async def test_another_polling_status_refreshes_the_stats_in_place(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """A move to generating report refreshes the stats card and strips the
    in-progress flag, while the poll keeps running."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    assert _status_line(client) == "Thinking"
    assert _stats_badges(client).get("Status") == "running"
    assert _header_text(_card(client, 3)) == "Iteration 3: Investigation in progress..."

    await _update_job(test_engine, job["job_id"], status="generating_report")
    await _tick(client, timer, wire_recorder)

    assert _stats_badges(client).get("Status") == "generating report"
    assert _stats_badges(client).get("Progress") == "3/5"
    assert _status_line(client) == "Generating report..."
    assert _header_text(_card(client, 3)) == "Iteration 3: Activity logged, but no summary recorded"
    assert timer.active is True


@pytest.mark.asyncio
async def test_an_agent_status_change_refreshes_only_the_status_line(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """A new agent status refreshes the stats card's status line and touches
    no card."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card1 = _card(client, 1)
    await _open_card(client, card1)
    shown1 = _shown_actions(card1)
    assert _status_line(client) == "Thinking"
    headers = {number: _header_text(_card(client, number)) for number in (1, 2, 3)}

    await _update_job(test_engine, job["job_id"], agent_status="Coding")
    await _tick(client, timer, wire_recorder)

    assert _status_line(client) == "Coding"
    for number in (1, 2, 3):
        assert _header_text(_card(client, number)) == headers[number]
    assert card1.value is True
    assert _shown_actions(card1) == shown1


@pytest.mark.asyncio
async def test_lost_access_stops_the_poll_and_replaces_the_page(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """Once the job row is gone, the next tick stops the poll and replaces
    the page with the access-lost notice."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        await db.execute(delete(Job).where(Job.id == UUID(job["job_id"])))
        await db.commit()

    await _tick(client, timer, wire_recorder)

    assert timer.active is False
    assert _iteration_cards(client) == []
    assert _status_line(client) is None
    with client:
        notice = [
            e
            for e in client.elements.values()
            if isinstance(e, ui.label) and e.text == "Access to this job has been revoked."
        ]
    assert notice, "the access-lost notice is missing"


@pytest.mark.asyncio
async def test_an_actionless_open_card_gains_its_actions_section(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
    test_engine: AsyncEngine,
) -> None:
    """An open iteration whose body loaded with no log rows gets its Actions
    section from the log once a row appears."""
    job = running_job
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        await db.execute(
            delete(AnalysisLog).where(
                AnalysisLog.job_id == UUID(job["job_id"]),
                AnalysisLog.iteration == 3,
            )
        )
        await db.commit()

    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)

    card3 = _card(client, 3)
    await _open_card(client, card3)
    assert _actions_expansion(card3) is None, "no log rows, no Actions section"

    await _add_log_row(test_engine, job["job_id"], iteration=3, step=1, description="Run 9")
    await _tick(client, timer, wire_recorder)

    assert card3.value is True
    assert _actions_label(card3) == "Actions (1)"
    assert _shown_actions(card3) == ["\u2705 Run 9"]


@pytest.mark.asyncio
async def test_a_deleted_client_skips_its_tick(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
    wire_recorder: list[WireMessage],
) -> None:
    """A deleted client's tick runs no statement and sends no update."""
    job = running_job
    client = await _build(served_page, job)
    client.tab_id = "wire-test-socket"
    await drain_outbox(client)
    timer = _timer(client)
    assert timer.callback is not None
    callback = timer.callback

    client.delete()
    await drain_outbox(client)
    wire_recorder.clear()
    with _Statements() as rec:
        with client:
            await callback()
    await drain_outbox(client)
    assert not [m for m in wire_recorder if m.event == "update" and m.room == client.id]
    assert not rec, "a deleted client's tick ran statements"


def _body_fragment(card: ui.expansion) -> str:
    """The card body's boxes as plain divs with their classes, each section's
    content a 40px block, so a layout engine can place the sections."""
    [content_box] = card.default_slot.children
    sections = "".join(
        f'<div id="section-{index}" class="{" ".join(section.classes)}">'
        + ("".join('<div style="height:40px"></div>' for _ in section.default_slot.children))
        + "</div>"
        for index, section in enumerate(content_box.default_slot.children)
    )
    return f'<div id="body" class="{" ".join(content_box.classes)}">{sections}</div>'


@pytest.mark.asyncio
async def test_a_card_with_only_actions_starts_with_them(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
) -> None:
    """Iteration 3 has neither a summary nor findings yet, so its Actions
    section sits at the top of the body with no space held for the others."""
    from openscientist.webapp_components.pages.job_detail.iterations import ITERATION_STYLES
    from tests.webapp.css_layout import box_by_id, layout

    client = await _build(served_page, running_job)
    card = _card(client, 3)
    await _open_card(client, card)
    [content_box] = card.default_slot.children
    sections = content_box.default_slot.children
    actions_index = next(i for i, s in enumerate(sections) if s.default_slot.children)
    boxes = layout(_body_fragment(card), ITERATION_STYLES)
    body = box_by_id(boxes, "body")
    actions = box_by_id(boxes, f"section-{actions_index}")
    assert actions.position_y == pytest.approx(body.position_y)


@pytest.mark.asyncio
async def test_sections_of_a_card_sit_a_small_gap_apart(
    database: Forwarder,
    running_job: dict[str, str],
    served_page: ServedPage,
) -> None:
    """Iteration 1 shows its summary and its actions, half a rem apart."""
    from openscientist.webapp_components.pages.job_detail.iterations import ITERATION_STYLES
    from tests.webapp.css_layout import box_by_id, layout

    client = await _build(served_page, running_job)
    card = _card(client, 1)
    await _open_card(client, card)
    [content_box] = card.default_slot.children
    filled = [i for i, s in enumerate(content_box.default_slot.children) if s.default_slot.children]
    assert len(filled) >= 2
    boxes = layout(_body_fragment(card), ITERATION_STYLES)
    first = box_by_id(boxes, f"section-{filled[0]}")
    second = box_by_id(boxes, f"section-{filled[1]}")
    assert second.position_y - (first.position_y + first.height) == pytest.approx(8)
