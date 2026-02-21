"""Skill detail page."""

import logging
from uuid import UUID

from nicegui import ui
from sqlalchemy import select

from shandy.auth import get_current_user_id, require_auth
from shandy.database.models import Skill, SkillSource
from shandy.database.rls import set_current_user
from shandy.database.session import get_session
from shandy.webapp_components.ui_components import (
    format_relative_time,
    get_category_color,
    render_error_state,
    render_loading_spinner,
    render_navigator,
    render_not_found_state,
)
from shandy.webapp_components.utils import setup_timer_cleanup

logger = logging.getLogger(__name__)


@ui.page("/skill/{category}/{slug}")
@require_auth
async def skill_detail_page(category: str, slug: str):
    """Skill detail page."""
    # Track active timers for cleanup on disconnect
    _active_timers = setup_timer_cleanup()

    # Page header with navigation
    render_navigator(active_page="skills")

    # Container for page content (populated async)
    content_container = ui.column().classes("w-full")

    async def load_skill():
        """Load skill from database."""
        try:
            user_id = get_current_user_id()
            async with get_session() as session:
                # Set RLS context
                await set_current_user(session, UUID(user_id))

                # Query skill by category and slug
                stmt = (
                    select(Skill, SkillSource)
                    .outerjoin(SkillSource, Skill.source_id == SkillSource.id)
                    .where(
                        Skill.category == category,
                        Skill.slug == slug,
                        Skill.is_enabled == True,  # noqa: E712
                    )
                )
                result = await session.execute(stmt)
                row = result.first()

                content_container.clear()

                if not row:
                    # 404 - skill not found
                    with content_container:
                        render_not_found_state(
                            title="Skill not found",
                            message=f"The skill '{category}/{slug}' does not exist or has been disabled.",
                            back_label="Back to Skills",
                            back_url="/skills",
                        )
                    return

                skill, source = row

                # Set page title
                ui.page_title(f"{skill.name} - SHANDY")

                cat_color = get_category_color(skill.category)

                with content_container:
                    # Breadcrumb navigation
                    with ui.row().classes("items-center gap-2 mb-4 text-sm"):
                        ui.link("Skills", "/skills").classes("text-cyan-600 hover:underline")
                        ui.label("/").classes("text-gray-400")
                        ui.badge(skill.category, color=cat_color).props("outline")
                        ui.label("/").classes("text-gray-400")
                        ui.label(skill.name).classes("text-gray-600")

                    # Metadata card
                    with ui.card().classes("w-full mb-4"):
                        with ui.row().classes("items-start justify-between flex-wrap"):
                            with ui.column().classes("flex-1 min-w-0"):
                                # Skill name with category badge
                                with ui.row().classes("items-center gap-3 mb-2"):
                                    ui.label(skill.name).classes("text-h5 font-bold")
                                    ui.badge(skill.category, color=cat_color)

                                # Description
                                if skill.description:
                                    ui.label(skill.description).classes("text-gray-600 mb-3")

                                # Tags
                                if skill.tags:
                                    with ui.row().classes("gap-2 flex-wrap"):
                                        for tag in skill.tags:
                                            ui.badge(tag, color="gray").props("outline dense")

                            # Metadata sidebar
                            with ui.column().classes("gap-2 ml-4"):
                                # Version
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon("tag", size="sm").classes("text-gray-400")
                                    ui.label(f"Version {skill.version}").classes(
                                        "text-sm text-gray-600"
                                    )

                                # Source info
                                if source:
                                    with ui.row().classes("items-center gap-2"):
                                        icon = (
                                            "public" if source.source_type == "github" else "folder"
                                        )
                                        ui.icon(icon, size="sm").classes("text-gray-400")
                                        with ui.column():
                                            ui.label(source.name).classes("text-sm font-medium")
                                            ui.label(source.source_type).classes(
                                                "text-xs text-gray-500"
                                            )

                                    # Last synced
                                    with ui.row().classes("items-center gap-2"):
                                        ui.icon("sync", size="sm").classes("text-gray-400")
                                        ui.label(
                                            format_relative_time(source.last_synced_at)
                                        ).classes("text-sm text-gray-600")
                                else:
                                    with ui.row().classes("items-center gap-2"):
                                        ui.icon("inventory_2", size="sm").classes("text-gray-400")
                                        ui.label("Built-in").classes("text-sm text-gray-600")

                    # Skill content
                    with ui.card().classes("w-full"):
                        ui.label("Content").classes("text-subtitle2 font-bold mb-2 text-gray-500")
                        ui.separator().classes("mb-4")
                        ui.markdown(skill.content).classes("skill-content")

                    # Back button
                    with ui.row().classes("w-full justify-start mt-4"):
                        ui.button(
                            "Back to Skills",
                            on_click=lambda: ui.navigate.to("/skills"),
                            icon="arrow_back",
                        ).props("flat color=primary")

        except Exception as e:
            logger.error("Failed to load skill %s/%s: %s", category, slug, e, exc_info=True)
            content_container.clear()
            with content_container:
                render_error_state(
                    title="Failed to load skill",
                    message="An unexpected error occurred. Please try again.",
                    back_label="Back to Skills",
                    back_url="/skills",
                )

    # Show loading state initially
    with content_container:
        render_loading_spinner("Loading skill...")

    # Load skill data
    init_timer = ui.timer(0.1, load_skill, once=True)
    _active_timers.append(init_timer)
