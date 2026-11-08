"""The compact action-row standard of the job page's Actions list.

Layout tests pin the standard against NiceGUI's and Quasar's real CSS
(one-line header row, capped code and output blocks), and page tests pin
the structure every action type renders under it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from nicegui import background_tasks, ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import openscientist.webapp_components.pages.job_detail.analysis_log as analysis_log
from openscientist import web_app
from openscientist.database.models import AnalysisLog, Job, Session, User
from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages.job_detail import job_detail_page
from openscientist.webapp_components.pages.job_detail.analysis_log import (
    RESULT_PREVIEW_CHARS,
    _action_facts,
    _action_icon,
    _inline_output,
)
from tests.db_outage import Forwarder
from tests.webapp.conftest import ServedPage
from tests.webapp.css_layout import box_by_id, layout

LONG_OUTPUT = "line of result\n" * 200
SHORT_OUTPUT = "(312, 24)"
FAILED_INLINE_OUTPUT = "Error: object 'y' not found"

#: Every action type the tools log, in the order the page lists them.
_ACTIONS: tuple[dict[str, object], ...] = (
    {"action_type": "set_job_title", "description": "Lipid response to hypothermia"},
    {"action_type": "set_status", "description": "Loading the primary dataset"},
    {
        "action_type": "execute_code",
        "description": "Load the primary dataset",
        "input_data": {"code": "print(data.shape)", "language": "python"},
        "output_data": {"output": SHORT_OUTPUT},
        "duration_seconds": 2.4,
    },
    {
        "action_type": "execute_code",
        "description": "Profile carnosine by condition",
        "input_data": {"code": "data.describe()\nprint(data)", "language": "python"},
        "output_data": {"output": LONG_OUTPUT},
        "duration_seconds": 41.7,
    },
    {
        "action_type": "execute_code",
        "description": "Fit PCA on the expression matrix",
        "input_data": {"code": "from sklearn.decomposition import PCA\nprint(PCA(2))"},
        "output_data": {
            "output": "ValueError: could not convert string to float: 'n/a'\nTraceback (most recent call last):"
        },
        "duration_seconds": 0.8,
        "success": False,
    },
    {"action_type": "add_hypothesis", "description": "Record H001"},
    {
        "action_type": "add_hypothesis",
        "description": "Record H001",
        "input_data": {"statement": "Hypothermia elevates carnosine levels in the plasma"},
    },
    {
        "action_type": "search_pubmed",
        "description": "Evidence for H001",
        "input_data": {"query": "carnosine neuroprotection hypothermia", "results_count": 14},
    },
    {
        "action_type": "search_pubmed",
        "description": "Search 1",
        "input_data": {"query": "metabolomics hypothermia rat", "results_count": 0},
    },
    {
        "action_type": "read_document",
        "description": "Read the protocol PDF",
        "input_data": {"file_path": "protocol.pdf"},
        "output_data": {"output": "STUDY PROTOCOL\n1. Animals were cooled to 33 C for 120 min."},
    },
    {
        "action_type": "update_hypothesis",
        "description": "Test H001",
        "input_data": {
            "hypothesis_id": "H001",
            "status": "supported",
            "result_summary": "t = 4.61, p = 0.003, d = 0.8",
        },
        "duration_seconds": 3.1,
    },
    {
        "action_type": "update_knowledge_state",
        "description": "Record the supported finding",
        "input_data": {
            "title": "Carnosine is elevated in hypothermic samples",
            "evidence": "p = 0.003, effect size 0.8",
        },
    },
    {
        "action_type": "run_phenix_tool",
        "description": "Full validation",
        "input_data": {"tool_name": "phenix.molprobity", "input_files": ["structure.pdb"]},
        "output_data": {"output": "MolProbity: 99.2% ideal rotamers.\nClashscore 2.1 (worst 1.8)."},
        "duration_seconds": 12.1,
    },
    {
        "action_type": "compare_structures",
        "description": "Superpose experimental and predicted",
        "input_data": {"experimental_pdb": "exp.pdb", "predicted_pdb": "af.pdb"},
        "output_data": {"output": "RMSD (CA): 1.42 A over 201 residues"},
    },
    {
        "action_type": "parse_alphafold_confidence",
        "description": "pLDDT profile",
        "input_data": {"alphafold_pdb": "af.pdb"},
        "output_data": {"output": "mean pLDDT 84.3, 2.1% of residues below 50"},
    },
    {
        "action_type": "save_iteration_summary",
        "description": "Summarise iteration 1",
        "input_data": {"summary": "Carnosine is elevated in hypothermic samples."},
    },
    {"action_type": "fetch_dataset_metadata", "description": "Fetch GEO metadata"},
    {
        "action_type": "execute_code",
        "description": "Run the R model",
        "input_data": {"code": "fit <- lm(y ~ x)", "language": "r"},
        "output_data": {"output": FAILED_INLINE_OUTPUT},
        "duration_seconds": 0.3,
        "success": False,
    },
)

EXPECTED_ICONS = [
    "title",
    "info",
    "code",
    "code",
    "code",
    "psychology",
    "psychology",
    "search",
    "search",
    "description",
    "psychology",
    "bolt",
    "science",
    "compare_arrows",
    "insights",
    "summary",
    "more_horiz",
    "code",
]

#: The rows the standard renders as a plain column, the rest behind an expand.
PLAIN_DESCRIPTIONS = {
    "Lipid response to hypothermia",
    "Loading the primary dataset",
    "Record H001",
    "Superpose experimental and predicted",
    "pLDDT profile",
    "Summarise iteration 1",
    "Fetch GEO metadata",
}

#: Tailwind utilities the renderer uses that WeasyPrint does not generate.
_TAILWIND_SHIM = """
<style>
.arow .items-center { align-items: center; }
.arow .no-wrap { white-space: nowrap; }
.arow .w-full { width: 100%; }
.arow .flex-1 { flex: 1 1 0%; min-width: 0; }
.arow.gap-0, .arow .gap-0 { gap: 0; }
.arow .gap-1\\.5 { gap: 0.375rem; }
</style>
"""


def _header_fragment(title: str, facts: str | None = None, suffix: str = "") -> str:
    facts_html = (
        f'<label id="arow-facts{suffix}" class="nicegui-label os-action-facts">{facts}</label>'
        if facts
        else ""
    )
    return (
        '<div class="os-action-header nicegui-row row w-full no-wrap items-center gap-1.5">'
        f'<i id="arow-icon{suffix}" class="q-icon notranslate material-icons os-action-icon">code</i>'
        '<span class="os-action-dot"></span>'
        f'<label id="arow-title{suffix}" class="nicegui-label os-action-title flex-1">{title}</label>'
        f"{facts_html}"
        "</div>"
    )


def _plain_row(title: str, facts: str | None = None, suffix: str = "") -> str:
    return (
        f'<div id="arow-plain{suffix}" class="arow os-action os-action-plain nicegui-column gap-0">'
        + _header_fragment(title, facts, suffix)
        + "</div>"
    )


def _expand_row(title: str, facts: str | None = None, suffix: str = "") -> str:
    """The row's expandable form: the header slot holds a column with the
    header row, and Quasar's own arrow section is the last flex item."""
    return (
        f'<div id="arow-exp{suffix}" class="arow os-action q-expansion-item">'
        '<div class="q-expansion-item__container relative-position">'
        '<div class="q-item q-item-type row no-wrap q-item--dense">'
        '<div class="os-action-header-col nicegui-column gap-0">'
        + _header_fragment(title, facts, suffix)
        + "</div>"
        '<div class="q-item__section column q-item__section--side justify-center">'
        '<i class="q-icon notranslate material-icons">expand_more</i>'
        "</div></div></div></div>"
    )


@pytest.mark.requires_weasyprint
def test_a_collapsed_action_row_stays_one_line_and_40px() -> None:
    """The row's standard header is one line and at most ~40px tall."""
    boxes = layout(
        _plain_row("Profile carnosine by condition", "14:32:05 &middot; 41.7s"),
        analysis_log.ACTION_STYLES,
        _TAILWIND_SHIM,
        page_width=1320,
    )
    row = box_by_id(boxes, "arow-plain")
    assert float(row.height) <= 40
    title = box_by_id(boxes, "arow-title")
    assert float(title.height) <= 20


@pytest.mark.requires_weasyprint
def test_a_long_title_stays_one_line_on_a_phone() -> None:
    boxes = layout(
        _plain_row("Profile carnosine by condition and check the p-values", None),
        analysis_log.ACTION_STYLES,
        _TAILWIND_SHIM,
        page_width=390,
    )
    title = box_by_id(boxes, "arow-title")
    assert float(title.height) <= 20


@pytest.mark.requires_weasyprint
def test_plain_and_expandable_rows_share_one_grid() -> None:
    """Both row forms fill the same grid: the icon starts at the same x and
    the facts end at the same right edge, the arrow owning the last 24px of
    the expandable row."""
    body = _plain_row("Profile carnosine by condition", "14:32:05 &middot; 41.7s", "-plain")
    body += _expand_row("Load the primary dataset", "13:27:56 &middot; 2.4s", "-exp")
    boxes = layout(body, analysis_log.ACTION_STYLES, _TAILWIND_SHIM, quasar=True, page_width=1320)
    plain_icon = box_by_id(boxes, "arow-icon-plain")
    expand_icon = box_by_id(boxes, "arow-icon-exp")
    assert abs(float(plain_icon.position_x) - float(expand_icon.position_x)) <= 1
    plain_facts = box_by_id(boxes, "arow-facts-plain")
    expand_facts = box_by_id(boxes, "arow-facts-exp")
    plain_edge = float(plain_facts.position_x) + float(plain_facts.width)
    expand_edge = float(expand_facts.position_x) + float(expand_facts.width)
    assert abs(plain_edge - expand_edge) <= 1


@pytest.mark.requires_weasyprint
def test_a_code_block_is_capped_at_12rem() -> None:
    """WeasyPrint clamps block content against max-height, so a tall body
    inside the code block must come out no taller than the cap."""
    body = (
        '<div id="arow" class="arow os-action os-action-execute">'
        '<div class="nicegui-code" id="arow-codeblock"><div style="height: 1000px"></div></div>'
        "</div>"
    )
    boxes = layout(body, analysis_log.ACTION_STYLES)
    code = box_by_id(boxes, "arow-codeblock")
    assert float(code.height) <= 12 * 16 + 8  # 12rem plus border


@pytest.mark.requires_weasyprint
def test_a_short_output_shows_inline_on_one_line_under_the_title() -> None:
    body = (
        '<div id="arow" class="arow os-action os-action-plain nicegui-column gap-0">'
        + _header_fragment("Load the primary dataset")
        + f'<label id="arow-inline" class="nicegui-label os-action-inline w-full">{SHORT_OUTPUT}</label>'
        + "</div>"
    )
    boxes = layout(body, analysis_log.ACTION_STYLES, _TAILWIND_SHIM, page_width=390)
    row = box_by_id(boxes, "arow")
    inline = box_by_id(boxes, "arow-inline")
    assert float(row.height) <= 48
    assert float(inline.height) <= 20
    title = box_by_id(boxes, "arow-title")
    inline_text_x = float(inline.position_x) + float(inline.padding_left)
    assert inline_text_x == pytest.approx(float(title.position_x), abs=1)


def test_every_logged_action_type_gets_a_known_icon() -> None:
    types = [str(spec["action_type"]) for spec in _ACTIONS]
    assert [_action_icon(t) for t in types] == EXPECTED_ICONS


def test_short_single_line_output_is_inline_and_the_rest_is_not() -> None:
    assert _inline_output({"output": SHORT_OUTPUT}) == SHORT_OUTPUT
    assert _inline_output({"output": LONG_OUTPUT}) is None
    assert _inline_output({"output": "line one\nline two"}) is None
    assert _inline_output({}) is None


def test_facts_join_time_and_duration() -> None:
    stamp = datetime(2026, 11, 8, 14, 32, 5, tzinfo=UTC).isoformat()
    assert _action_facts({"timestamp": stamp, "execution_time": 41.7}) == "14:32:05 \u00b7 41.7s"
    assert _action_facts({"timestamp": stamp}) == "14:32:05"
    assert _action_facts({"execution_time": 2.0}) == "2.0s"
    assert _action_facts({}) is None


@pytest_asyncio.fixture
async def all_types_job(test_engine: AsyncEngine) -> AsyncIterator[dict[str, str]]:
    """A running job whose single iteration logs one of every action type."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        owner = User(email=f"owner-{uuid4().hex}@rows.test", name="Owner", is_approved=True)
        db.add(owner)
        await db.flush()
        job = Job(
            owner_id=owner.id,
            research_question="Action rows question",
            status="running",
            current_iteration=1,
            max_iterations=3,
        )
        db.add(job)
        await db.flush()
        start = datetime.now(UTC) - timedelta(hours=1)
        rows = [
            AnalysisLog(
                job_id=job.id,
                iteration=1,
                step_number=number + 1,
                created_at=start + timedelta(minutes=number),
                **spec,
            )
            for number, spec in enumerate(_ACTIONS)
        ]
        db.add_all(rows)
        login = Session(user_id=owner.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield {
        "job_id": str(job.id),
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


def _of_type[E: ui.element](client: Client, kind: type[E]) -> list[E]:
    with client:
        return [e for e in client.elements.values() if isinstance(e, kind) and not e.is_deleted]


def _with_class(elements: list[ui.element], klass: str) -> list[ui.element]:
    return [e for e in elements if klass in e.classes]


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


async def _until(condition, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, f"{what} within 10 s"
        await asyncio.sleep(0.02)


async def _served(served_page: ServedPage, all_types_job: dict[str, str]) -> Client:
    client, _ = await served_page(
        all_types_job["token"],
        lambda: job_detail_page(all_types_job["job_id"]),
        lambda c: None,
    )
    return client


async def _open_actions(client: Client) -> ui.expansion:
    """Open the iteration and its Actions list."""
    with client:
        expansion = _expansion_named(client, "Iteration 1:")
        before = set(background_tasks.running_tasks)
        expansion.value = True
    for task in background_tasks.running_tasks - before:
        await asyncio.wait_for(task, 10)
    actions = _expansion_named(client, "Actions")
    await _until(
        lambda: (
            len(
                _with_class(
                    [c for c in actions.descendants() if isinstance(c, ui.label)],
                    "os-action-title",
                )
            )
            == len(_ACTIONS)
        ),
        "every action row",
    )
    return actions


def _row_of_title(client: Client, title: str) -> ui.element:
    """The row container of the action whose header shows ``title``."""
    actions = _expansion_named(client, "Actions")
    for child in actions.descendants():
        # Exact row classes; the list column's ``os-actions`` must not match.
        if not any(c == "os-action" or c.startswith("os-action-") for c in child.classes):
            continue
        labels = [e for e in child.descendants() if isinstance(e, ui.label)]
        if any("os-action-title" in e.classes and e.text == title for e in labels):
            return child
    raise AssertionError(f"no action row for {title!r}")


@pytest.mark.asyncio
async def test_every_action_renders_its_header_row(
    database: Forwarder, all_types_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, all_types_job)
    actions = await _open_actions(client)
    descendants = list(actions.descendants())
    titles = [
        e.text for e in descendants if isinstance(e, ui.label) and "os-action-title" in e.classes
    ]
    assert titles == [str(spec["description"]) for spec in _ACTIONS]
    icons = [
        icon.props.get("name")
        for icon in descendants
        if isinstance(icon, ui.icon) and "os-action-icon" in icon.classes
    ]
    assert icons == EXPECTED_ICONS
    tooltips = [e.text for e in descendants if isinstance(e, ui.tooltip)]
    assert tooltips == [str(spec["action_type"]) for spec in _ACTIONS]
    facts = [
        e.text for e in descendants if isinstance(e, ui.label) and "os-action-facts" in e.classes
    ]
    assert len(facts) == len(_ACTIONS)
    assert "2.4s" in facts[2] and "41.7s" in facts[3]


@pytest.mark.asyncio
async def test_entries_with_details_expand_and_the_rest_stay_plain(
    database: Forwarder, all_types_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, all_types_job)
    await _open_actions(client)
    for spec in _ACTIONS:
        title = str(spec["description"])
        row = _row_of_title(client, title)
        if title in PLAIN_DESCRIPTIONS:
            assert not isinstance(row, ui.expansion), title
        else:
            assert isinstance(row, ui.expansion), title
            with client:
                assert row.value is False, f"{title} must start collapsed"


@pytest.mark.asyncio
async def test_short_output_shows_inline_and_long_output_is_capped(
    database: Forwarder, all_types_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, all_types_job)
    await _open_actions(client)
    descendants = list(_expansion_named(client, "Actions").descendants())
    inlines = [
        e.text for e in descendants if isinstance(e, ui.label) and "os-action-inline" in e.classes
    ]
    assert inlines == [
        SHORT_OUTPUT,
        "RMSD (CA): 1.42 A over 201 residues",
        "mean pLDDT 84.3, 2.1% of residues below 50",
        FAILED_INLINE_OUTPUT,
    ]
    outputs = [
        code.content
        for code in descendants
        if isinstance(code, ui.code) and "os-action-output" in code.classes
    ]
    assert len(outputs) == 4
    assert LONG_OUTPUT[:RESULT_PREVIEW_CHARS] + "..." in outputs
    assert SHORT_OUTPUT not in outputs
    row = _row_of_title(client, "Load the primary dataset")
    assert isinstance(row, ui.expansion)
    with client:
        row.value = True
    assert not any(
        e.text == SHORT_OUTPUT
        for e in row.descendants()
        if isinstance(e, ui.label) and "os-action-meta" in e.classes
    )


@pytest.mark.asyncio
async def test_a_failed_action_renders_its_failure_and_keeps_its_details(
    database: Forwarder, all_types_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, all_types_job)
    await _open_actions(client)
    row = _row_of_title(client, "Fit PCA on the expression matrix")
    assert isinstance(row, ui.expansion)
    title = next(
        e for e in row.descendants() if isinstance(e, ui.label) and "os-action-title" in e.classes
    )
    assert "os-action-title-failed" in title.classes
    assert any("os-action-dot-failed" in e.classes for e in row.descendants())
    with client:
        row.value = True
    outputs = [
        code.content
        for code in row.descendants()
        if isinstance(code, ui.code) and "os-action-output" in code.classes
    ]
    assert outputs and outputs[0].startswith("ValueError: could not convert")
    codes = [
        code.content
        for code in row.descendants()
        if isinstance(code, ui.code) and "os-action-code" in code.classes
    ]
    assert codes and "sklearn.decomposition" in codes[0]


@pytest.mark.asyncio
async def test_a_failed_short_output_shows_inline_in_red_with_its_language(
    database: Forwarder, all_types_job: dict[str, str], served_page: ServedPage
) -> None:
    client = await _served(served_page, all_types_job)
    await _open_actions(client)
    row = _row_of_title(client, "Run the R model")
    assert isinstance(row, ui.expansion)
    inline = next(
        e for e in row.descendants() if isinstance(e, ui.label) and "os-action-inline" in e.classes
    )
    assert inline.text == FAILED_INLINE_OUTPUT
    assert "os-action-inline-failed" in inline.classes
    with client:
        row.value = True
    metas = [
        e.text
        for e in row.descendants()
        if isinstance(e, ui.label) and "os-action-meta" in e.classes
    ]
    assert metas == ["Language: r"]
