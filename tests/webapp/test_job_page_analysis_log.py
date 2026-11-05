"""The Research Log of a job page, built in a real signed-in NiceGUI client,
against an analysis log too long to show at once.

Iteration 1 holds 110 code runs, each with a result longer than the page
shows, followed by 10 literature searches. Iteration 2 holds 3 findings.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import AnalysisLog, Job, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from openscientist.webapp_components.pages.job_detail.analysis_log import _IterationLog
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage

CODE_RUNS = 110
SEARCHES = 10
FINDINGS = 3
RESULT = "r" * 5000


@pytest_asyncio.fixture
async def long_log_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A completed two-iteration job, its owner's live session and a user
    without access."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@log.test", name="Owner", is_approved=True)
        stranger = User(email=f"stranger-{uuid4().hex}@log.test", name="Other", is_approved=True)
        db.add_all([owner, stranger])
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="Long log question",
            status="completed",
            current_iteration=2,
            max_iterations=2,
        )
        db.add(job)
        await db.flush()
        start = datetime.now(UTC) - timedelta(days=1)
        rows = [
            AnalysisLog(
                job_id=job.id,
                iteration=1,
                step_number=1,
                action_type="execute_code",
                description=f"Run {n}",
                input_data={"code": f"print({n})"},
                output_data={"output": RESULT},
                created_at=start + timedelta(seconds=n),
            )
            for n in range(CODE_RUNS)
        ]
        rows += [
            AnalysisLog(
                job_id=job.id,
                iteration=1,
                step_number=1,
                action_type="search_pubmed",
                description=f"Search {n}",
                input_data={"query": f"query {n}", "results_count": 2},
                created_at=start + timedelta(seconds=CODE_RUNS + n),
            )
            for n in range(SEARCHES)
        ]
        rows += [
            AnalysisLog(
                job_id=job.id,
                iteration=2,
                step_number=1,
                action_type="update_knowledge_state",
                description=f"Finding {n}",
                input_data={"title": f"Finding {n}"},
                created_at=start + timedelta(hours=1, seconds=n),
            )
            for n in range(FINDINGS)
        ]
        db.add_all(rows)
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {
        "job_id": str(job.id),
        "owner_id": str(owner.id),
        "stranger_id": str(stranger.id),
        "token": str(login.id),
    }
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(Job).where(Job.id == job.id))
        await db.execute(delete(User).where(User.id.in_([owner.id, stranger.id])))
        await db.commit()


@pytest.fixture(autouse=True)
def _job_manager(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The page reads the job directory and the latest job from the job manager."""

    async def get_job(job_id: str) -> SimpleNamespace:
        return SimpleNamespace(status=JobStatus.COMPLETED)

    monkeypatch.setattr(
        web_app._state, "job_manager", SimpleNamespace(jobs_dir=tmp_path, get_job=get_job)
    )


def _of_type[E: ui.element](client: Client, kind: type[E]) -> list[E]:
    with client:
        return [e for e in client.elements.values() if isinstance(e, kind) and not e.is_deleted]


def _expansion_named(client: Client, prefix: str) -> ui.expansion:
    """The expansion whose own text or header label starts with ``prefix``."""
    for expansion in _of_type(client, ui.expansion):
        if (expansion.props.get("label") or "").startswith(prefix):
            return expansion
        header = expansion.slots.get("header")
        if header and any(
            isinstance(child, ui.label) and child.text.startswith(prefix)
            for element in header.children
            for child in [element, *element.descendants()]
        ):
            return expansion
    raise AssertionError(f"no expansion starts with {prefix!r}")


def _action_titles(client: Client) -> list[str]:
    """The titles of the action cards inside the iteration's Actions list."""
    actions = _expansion_named(client, "Actions")
    return [
        child.text
        for child in actions.descendants()
        if isinstance(child, ui.label) and child.text.startswith("\u2705 ")
    ]


def _show_more(client: Client) -> ui.button | None:
    buttons = [b for b in _of_type(client, ui.button) if b.text == "Show more"]
    return buttons[0] if buttons else None


async def _until(condition, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, f"{what} within 10 s"
        await asyncio.sleep(0.02)


async def _open_iteration(client: Client, number: int) -> None:
    with client:
        _expansion_named(client, f"Iteration {number}:").value = True
    await _until(lambda: _actions_shown(client), f"iteration {number} opened")


def _actions_shown(client: Client) -> bool:
    try:
        _expansion_named(client, "Actions")
    except AssertionError:
        return False
    return True


def _click(client: Client, button: ui.button) -> None:
    """Fire the button's click the way NiceGUI's socket handler does."""
    listener = next(iter(button._event_listeners.values()))
    assert listener.handler is not None
    with client, button.parent_slot or nullcontext():
        listener.handler(None)


async def _served(served_page: ServedPage, job: dict[str, str]) -> Client:
    client, _ = await served_page(
        job["token"], lambda: job_detail_page(job["job_id"]), lambda c: None
    )
    return client


@pytest.mark.asyncio
async def test_each_iteration_header_counts_its_actions(
    database: Forwarder, long_log_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, long_log_job)
    badges = {badge.text for badge in _of_type(client, ui.badge)}
    assert {f"{CODE_RUNS} analyses", f"{SEARCHES} searches", f"{FINDINGS} findings"} <= badges


@pytest.mark.asyncio
async def test_an_opened_iteration_shows_its_actions_fifty_at_a_time(
    database: Forwarder, long_log_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, long_log_job)
    await _open_iteration(client, 1)
    total = CODE_RUNS + SEARCHES
    assert _expansion_named(client, "Actions").props["label"] == f"Actions ({total})"
    assert _action_titles(client) == [f"\u2705 Run {n}" for n in range(50)]

    for shown in (100, total):
        button = _show_more(client)
        assert button is not None
        _click(client, button)
        await _until(lambda n=shown: len(_action_titles(client)) >= n, f"{shown} actions shown")
        assert len(_action_titles(client)) == shown

    titles = _action_titles(client)
    assert titles[-SEARCHES:] == [f"\u2705 Search {n}" for n in range(SEARCHES)]
    assert titles[:CODE_RUNS] == [f"\u2705 Run {n}" for n in range(CODE_RUNS)]
    assert _show_more(client) is None


@pytest.mark.asyncio
async def test_an_opened_iteration_lists_every_search_beyond_the_first_actions(
    database: Forwarder, long_log_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, long_log_job)
    await _open_iteration(client, 1)
    literature = _expansion_named(client, "Literature searched")
    assert literature.props["label"] == f"Literature searched ({SEARCHES * 2} papers)"
    queries = [child.text for child in literature.descendants() if isinstance(child, ui.label)]
    assert queries == [f'"query {n}" (0 results)' for n in range(SEARCHES)]


@pytest.mark.asyncio
async def test_a_long_result_shows_only_its_first_2000_characters(
    database: Forwarder, long_log_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, long_log_job)
    await _open_iteration(client, 1)
    results = [code.content for code in _of_type(client, ui.code) if code.content.startswith("r")]
    assert results == [RESULT[:2000] + "..."] * 50


@pytest.mark.asyncio
async def test_an_iterations_log_is_read_only_for_who_may_see_the_job(
    database: Forwarder, long_log_job: dict[str, str]
) -> None:
    job_id = long_log_job["job_id"]
    owners = _IterationLog(job_id, long_log_job["owner_id"], 2)
    strangers = _IterationLog(job_id, long_log_job["stranger_id"], 2)
    assert [a["description"] for a in await owners.actions(0)] == [
        f"Finding {n}" for n in range(FINDINGS)
    ]
    assert await strangers.actions(0) == []
    assert await strangers.searches() == []
