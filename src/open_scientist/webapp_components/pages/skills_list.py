"""Skills list page."""

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from nicegui import ui
from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

from shandy.auth import get_current_user_id, require_auth
from shandy.database.models import Skill, SkillSource
from shandy.database.rls import set_current_user
from shandy.database.session import get_session_ctx
from shandy.webapp_components.ui_components import (
    format_relative_time,
    get_category_color,
    render_empty_state,
    render_navigator,
    render_skill_name_slot,
)
from shandy.webapp_components.utils import setup_timer_cleanup

logger = logging.getLogger(__name__)

_SKILL_COLUMNS: list[dict[str, Any]] = [
    {"name": "name", "label": "Name", "field": "name", "align": "left", "sortable": True},
    {"name": "category", "label": "Category", "field": "category", "align": "center"},
    {"name": "source", "label": "Source", "field": "source", "align": "left"},
    {"name": "last_synced", "label": "Last Synced", "field": "last_synced", "align": "left"},
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


@ui.page("/skills")
@require_auth
async def skills_page() -> None:
    """Skills list page."""
    ui.page_title("Skills - SHANDY")
    _active_timers = setup_timer_cleanup()
    render_navigator(active_page="skills")

    state: dict[str, Any] = {"search": "", "category": None, "categories": []}

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
        skills_table.add_slot(
            "body-cell-category",
            r"""
            <q-td :props="props">
                <q-badge :color="props.row.category_color" outline>
                    {{ props.row.category }}
                </q-badge>
            </q-td>
            """,
        )
        skills_table.on(
            "view-skill",
            lambda e: ui.navigate.to(f"/skill/{e.args['category']}/{e.args['slug']}"),
        )
        empty_container = ui.column().classes("w-full hidden")

    def clear_search() -> None:
        """Clear search and trigger async reload."""
        search_input.value = ""
        state["search"] = ""
        ui.timer(0.1, load_skills, once=True)

    async def load_categories() -> None:
        """Load available categories and refresh category dropdown."""
        try:
            categories = await _load_categories()
            state["categories"] = categories
            category_select.options = categories
            category_select.update()
        except Exception as exc:
            logger.error("Failed to load categories: %s", exc, exc_info=True)

    async def load_skills() -> None:
        """Load skills from database with active filters."""
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

    async def on_search_change(e: Any) -> None:
        state["search"] = e.value or ""
        await load_skills()

    async def on_category_change(e: Any) -> None:
        state["category"] = e.value
        await load_skills()

    search_input.on("update:model-value", on_search_change)
    category_select.on("update:model-value", on_category_change)

    async def initial_load() -> None:
        await load_categories()
        await load_skills()

    _active_timers.append(ui.timer(0.1, initial_load, once=True))
