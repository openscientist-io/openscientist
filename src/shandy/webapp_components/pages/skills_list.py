"""Skills list page."""

import logging
from uuid import UUID

from nicegui import ui
from sqlalchemy import func, select

from shandy.auth import get_current_user_id, require_auth
from shandy.database.models import Skill, SkillSource
from shandy.database.rls import set_current_user
from shandy.database.session import get_session
from shandy.webapp_components.ui_components import (
    format_relative_time,
    get_category_color,
    render_empty_state,
    render_navigator,
    render_skill_name_slot,
)
from shandy.webapp_components.utils import setup_timer_cleanup

logger = logging.getLogger(__name__)


@ui.page("/skills")
@require_auth
async def skills_page():
    """Skills list page."""
    # Page title
    ui.page_title("Skills - SHANDY")

    # Track active timers for cleanup on disconnect
    _active_timers = setup_timer_cleanup()

    # Page header with navigation
    render_navigator(active_page="skills")

    # State for search and filtering
    state = {
        "search": "",
        "category": None,
        "categories": [],
    }

    # Forward declarations for event handlers (defined later)
    search_input = None
    category_select = None

    async def on_search_change(e):
        """Handle search input change."""
        state["search"] = e.value or ""
        await load_skills()

    async def on_category_change(e):
        """Handle category filter change."""
        state["category"] = e.value
        await load_skills()

    # Skills table container
    with ui.column().classes("w-full"):
        # Search and filter row
        with ui.row().classes("w-full gap-4 mb-4 items-end flex-wrap"):
            search_input = ui.input(
                label="Search skills",
                placeholder="Search by name, description, or content...",
                on_change=on_search_change,
            ).classes("flex-grow min-w-64")
            search_input.props("clearable outlined dense")

            category_select = ui.select(
                options=[],
                label="Category",
                value=None,
                on_change=on_category_change,
            ).classes("min-w-48")
            category_select.props("clearable outlined dense")

        # Skills table
        skills_table = ui.table(
            columns=[
                {
                    "name": "name",
                    "label": "Name",
                    "field": "name",
                    "align": "left",
                    "sortable": True,
                },
                {
                    "name": "category",
                    "label": "Category",
                    "field": "category",
                    "align": "center",
                },
                {
                    "name": "source",
                    "label": "Source",
                    "field": "source",
                    "align": "left",
                },
                {
                    "name": "last_synced",
                    "label": "Last Synced",
                    "field": "last_synced",
                    "align": "left",
                },
            ],
            rows=[],
            row_key="id",
            pagination=10,
        ).classes("w-full")

        # Add skill name column slot with clickable link
        skills_table.add_slot("body-cell-name", render_skill_name_slot())

        # Add category badge slot
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

        # Handle skill click - navigate to detail page
        skills_table.on(
            "view-skill",
            lambda e: ui.navigate.to(f"/skill/{e.args['category']}/{e.args['slug']}"),
        )

        # Empty state container (shown when no skills)
        empty_container = ui.column().classes("w-full hidden")

    async def load_categories():
        """Load all available categories."""
        try:
            user_id = get_current_user_id()
            async with get_session() as session:
                # Set RLS context
                await set_current_user(session, UUID(user_id))

                stmt = (
                    select(Skill.category)
                    .where(Skill.is_enabled == True)  # noqa: E712
                    .distinct()
                    .order_by(Skill.category)
                )
                result = await session.execute(stmt)
                categories = [row[0] for row in result.all()]
                state["categories"] = categories

                # Update category select options (simple list of strings)
                category_select.options = categories
                category_select.update()
        except Exception as e:
            logger.error("Failed to load categories: %s", e)

    async def load_skills():
        """Load skills with current search and filter."""
        try:
            user_id = get_current_user_id()
            async with get_session() as session:
                # Set RLS context
                await set_current_user(session, UUID(user_id))

                # Build query
                if state["search"]:
                    # Use full-text search
                    tsquery = func.plainto_tsquery("english", state["search"])
                    conditions = [
                        Skill.is_enabled == True,  # noqa: E712
                        Skill.search_vector.op("@@")(tsquery),
                    ]
                    if state["category"]:
                        conditions.append(Skill.category == state["category"])

                    stmt = (
                        select(Skill, SkillSource)
                        .outerjoin(SkillSource, Skill.source_id == SkillSource.id)
                        .where(*conditions)
                        .order_by(func.ts_rank(Skill.search_vector, tsquery).desc())
                        .limit(100)
                    )
                else:
                    # Regular query
                    conditions = [Skill.is_enabled == True]  # noqa: E712
                    if state["category"]:
                        conditions.append(Skill.category == state["category"])

                    stmt = (
                        select(Skill, SkillSource)
                        .outerjoin(SkillSource, Skill.source_id == SkillSource.id)
                        .where(*conditions)
                        .order_by(Skill.category, Skill.name)
                        .limit(100)
                    )

                result = await session.execute(stmt)
                rows_data = result.all()

                # Build table rows
                rows = []
                for skill, source in rows_data:
                    # Format source info
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

                skills_table.rows = rows
                skills_table.update()

                # Show/hide empty state
                if not rows:
                    skills_table.classes(add="hidden")
                    empty_container.classes(remove="hidden")
                    empty_container.clear()
                    with empty_container:
                        if state["search"]:
                            with ui.column().classes("w-full items-center py-8"):
                                ui.icon("search_off", size="xl").classes("text-gray-300 mb-4")
                                ui.label("No skills match your search").classes(
                                    "text-lg text-gray-600"
                                )
                                ui.button(
                                    "Clear search",
                                    on_click=lambda: clear_search(),
                                    icon="clear",
                                ).props("flat color=primary")
                        else:
                            render_empty_state("No skills available yet.")
                else:
                    skills_table.classes(remove="hidden")
                    empty_container.classes(add="hidden")

        except Exception as e:
            logger.error("Failed to load skills: %s", e, exc_info=True)
            ui.notify("Failed to load skills. Please try again.", type="negative")

    def clear_search():
        """Clear search input and reload."""
        search_input.value = ""
        state["search"] = ""
        ui.timer(0.1, load_skills, once=True)

    # Initial load - async wrapper to properly await both functions
    async def initial_load():
        await load_categories()
        await load_skills()

    init_timer = ui.timer(0.1, initial_load, once=True)
    _active_timers.append(init_timer)
