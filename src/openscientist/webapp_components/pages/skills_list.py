"""Skills & Expert Agents page."""

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from nicegui import ui
from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

from openscientist.auth import get_current_user_id, require_auth
from openscientist.database.models import Expert, Skill, SkillSource
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_session_ctx
from openscientist.webapp_components.ui_components import (
    format_relative_time,
    get_category_color,
    render_empty_state,
    render_navigator,
    render_skill_name_slot,
)
from openscientist.webapp_components.utils import get_event_value, setup_timer_cleanup

logger = logging.getLogger(__name__)

# -- Skills table columns --

_SKILL_COLUMNS: list[dict[str, Any]] = [
    {"name": "name", "label": "Name", "field": "name", "align": "left", "sortable": True},
    {"name": "category", "label": "Category", "field": "category", "align": "center"},
    {"name": "source", "label": "Source", "field": "source", "align": "left"},
    {"name": "last_synced", "label": "Last Synced", "field": "last_synced", "align": "left"},
]

# -- Expert Agents table columns --

_EXPERT_COLUMNS: list[dict[str, Any]] = [
    {"name": "name", "label": "Name", "field": "name", "align": "left", "sortable": True},
    {
        "name": "description",
        "label": "Description",
        "field": "description",
        "align": "left",
    },
    {"name": "category", "label": "Category", "field": "category", "align": "center"},
    {"name": "source", "label": "Source", "field": "source", "align": "left"},
]


def _skill_rows(rows_data: list[tuple[Skill, SkillSource | None]]) -> list[dict[str, Any]]:
    """Serialize query rows for skills table."""
    rows: list[dict[str, Any]] = []
    for skill, source in rows_data:
        if source:
            source_name = source.name
            source_type = f"({source.source_type})"
            last_synced = format_relative_time(source.last_synced_at)
        else:
            source_name = "Built-in"
            source_type = ""
            last_synced = "-"

        rows.append(
            {
                "id": str(skill.id),
                "name": skill.name,
                "slug": skill.slug,
                "category": skill.category,
                "category_color": get_category_color(skill.category),
                "description": skill.description or "",
                "source": f"{source_name} {source_type}".strip(),
                "last_synced": last_synced,
            }
        )
    return rows


def _expert_rows(experts: list[Expert]) -> list[dict[str, Any]]:
    """Serialize Expert rows for the experts table."""
    return [
        {
            "slug": e.slug,
            "name": e.name,
            "description": (
                e.description[:120] + "..." if len(e.description) > 120 else e.description
            ),
            "category": e.category,
            "category_color": get_category_color(e.category),
            "source": e.source,
        }
        for e in experts
    ]


def _skills_stmt(search: str, category: str | None) -> Any:
    """Build skill listing query from current filters."""
    conditions: list[ColumnElement[bool]] = [Skill.is_enabled.is_(True)]
    if category:
        conditions.append(Skill.category == category)

    if search:
        tsquery = func.plainto_tsquery("english", search)
        conditions.append(Skill.search_vector.op("@@")(tsquery))
        return (
            select(Skill, SkillSource)
            .outerjoin(SkillSource, Skill.source_id == SkillSource.id)
            .where(*conditions)
            .order_by(func.ts_rank(Skill.search_vector, tsquery).desc())
            .limit(100)
        )

    return (
        select(Skill, SkillSource)
        .outerjoin(SkillSource, Skill.source_id == SkillSource.id)
        .where(*conditions)
        .order_by(Skill.category, Skill.name)
        .limit(100)
    )


async def _load_categories() -> list[str]:
    """Load distinct enabled skill categories for filter dropdown."""
    user_id = get_current_user_id()
    async with get_session_ctx() as session:
        await set_current_user(session, UUID(user_id))
        result = await session.execute(
            select(Skill.category)
            .where(Skill.is_enabled.is_(True))
            .distinct()
            .order_by(Skill.category)
        )
    return [row[0] for row in result.all()]


async def _load_skills(search: str, category: str | None) -> list[dict[str, Any]]:
    """Load skill rows using current filters."""
    user_id = get_current_user_id()
    async with get_session_ctx() as session:
        await set_current_user(session, UUID(user_id))
        result = await session.execute(_skills_stmt(search, category))
    return _skill_rows(list(result.tuples().all()))


async def _load_experts() -> list[dict[str, Any]]:
    """Load enabled expert agents (public catalog, no per-user scoping)."""
    async with get_session_ctx() as session:
        result = await session.execute(
            select(Expert).where(Expert.is_enabled.is_(True)).order_by(Expert.slug)
        )
    return _expert_rows(list(result.scalars().all()))


def _render_no_results(
    *,
    empty_container: ui.element,
    has_search: bool,
    clear_search: Callable[[], None],
) -> None:
    """Render empty-state section for no matching skills."""
    empty_container.clear()
    with empty_container:
        if has_search:
            with ui.column().classes("w-full items-center py-8"):
                ui.icon("search_off", size="xl").classes("text-gray-300 mb-4")
                ui.label("No skills match your search").classes("text-lg text-gray-600")
                ui.button("Clear search", on_click=clear_search, icon="clear").props(
                    "flat color=primary"
                )
            return
        render_empty_state("No skills available yet.")


def _apply_table_visibility(
    *,
    skills_table: ui.table,
    empty_container: ui.element,
    rows: list[dict[str, Any]],
    has_search: bool,
    clear_search: Callable[[], None],
) -> None:
    """Show table rows or empty state depending on query output."""
    skills_table.rows = rows
    skills_table.update()
    if rows:
        skills_table.classes(remove="hidden")
        empty_container.classes(add="hidden")
        return
    skills_table.classes(add="hidden")
    empty_container.classes(remove="hidden")
    _render_no_results(
        empty_container=empty_container,
        has_search=has_search,
        clear_search=clear_search,
    )


_CATEGORY_BADGE_SLOT = r"""
<q-td :props="props">
    <q-badge :color="props.row.category_color" outline>
        {{ props.row.category }}
    </q-badge>
</q-td>
"""


@ui.page("/skills")
@require_auth
async def skills_page() -> None:
    """Skills & Expert Agents page."""
    ui.page_title("Skills & Expert Agents - OpenScientist")
    _active_timers = setup_timer_cleanup()
    render_navigator(active_page="skills")

    state: dict[str, Any] = {"search": "", "category": None, "categories": []}

    # -- Tab bar --
    with ui.tabs().classes("w-full") as tabs:
        ui.tab("skills", label="Skills", icon="auto_stories")
        ui.tab("experts", label="Expert Agents", icon="psychology")

    with ui.tab_panels(tabs, value="skills").classes("w-full"):
        # ── Skills panel ──────────────────────────────────────────
        with ui.tab_panel("skills"):
            with ui.column().classes("w-full"):
                with ui.row().classes("w-full gap-4 mb-4 items-end flex-wrap"):
                    search_input = ui.input(
                        label="Search skills",
                        placeholder="Search by name, description, or content...",
                    ).classes("flex-grow min-w-64")
                    search_input.props("clearable outlined dense")

                    category_select = ui.select(
                        options=[],
                        label="Category",
                        value=None,
                    ).classes("min-w-48")
                    category_select.props("clearable outlined dense")

                skills_table = ui.table(
                    columns=_SKILL_COLUMNS,
                    rows=[],
                    row_key="id",
                    pagination=10,
                ).classes("w-full")
                skills_table.add_slot("body-cell-name", render_skill_name_slot())
                skills_table.add_slot("body-cell-category", _CATEGORY_BADGE_SLOT)
                skills_table.on(
                    "view-skill",
                    lambda e: ui.navigate.to(f"/skill/{e.args['category']}/{e.args['slug']}"),
                )
                empty_container = ui.column().classes("w-full hidden")

        # ── Expert Agents panel ───────────────────────────────────
        with ui.tab_panel("experts"):
            with ui.column().classes("w-full"):
                experts_table = ui.table(
                    columns=_EXPERT_COLUMNS,
                    rows=[],
                    row_key="slug",
                ).classes("w-full")
                experts_table.add_slot("body-cell-category", _CATEGORY_BADGE_SLOT)
                experts_empty = ui.column().classes("w-full hidden")

    # -- Event handlers --

    def clear_search() -> None:
        search_input.value = ""
        state["search"] = ""
        ui.timer(0.1, load_skills, once=True)

    async def load_categories() -> None:
        try:
            categories = await _load_categories()
            state["categories"] = categories
            category_select.options = categories
            category_select.update()
        except Exception as exc:
            logger.error("Failed to load categories: %s", exc, exc_info=True)

    async def load_skills() -> None:
        try:
            rows = await _load_skills(state["search"], state["category"])
            _apply_table_visibility(
                skills_table=skills_table,
                empty_container=empty_container,
                rows=rows,
                has_search=bool(state["search"]),
                clear_search=clear_search,
            )
        except Exception as exc:
            logger.error("Failed to load skills: %s", exc, exc_info=True)
            ui.notify("Failed to load skills. Please try again.", type="negative")

    async def load_experts() -> None:
        try:
            rows = await _load_experts()
            experts_table.rows = rows
            experts_table.update()
            if rows:
                experts_table.classes(remove="hidden")
                experts_empty.classes(add="hidden")
            else:
                experts_table.classes(add="hidden")
                experts_empty.classes(remove="hidden")
                experts_empty.clear()
                with experts_empty:
                    render_empty_state("No expert agents available yet.")
        except Exception as exc:
            logger.error("Failed to load experts: %s", exc, exc_info=True)
            ui.notify("Failed to load expert agents. Please try again.", type="negative")

    async def on_search_change(e: Any) -> None:
        state["search"] = get_event_value(e) or ""
        await load_skills()

    async def on_category_change(e: Any) -> None:
        state["category"] = get_event_value(e)
        await load_skills()

    search_input.on("update:model-value", on_search_change)
    category_select.on("update:model-value", on_category_change)

    async def initial_load() -> None:
        await load_categories()
        await load_skills()
        await load_experts()

    _active_timers.append(ui.timer(0.1, initial_load, once=True))
