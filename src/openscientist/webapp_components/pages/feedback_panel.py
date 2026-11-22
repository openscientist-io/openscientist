"""Admin panel with the feedback users send and whether an admin has read it."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from uuid import UUID

from nicegui import ui
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import User, UserFeedback
from openscientist.webapp_components.components.banners import render_alert_banner
from openscientist.webapp_components.components.common import (
    render_dialog_actions,
    render_empty_state,
)
from openscientist.webapp_components.utils import in_page_client
from openscientist.webapp_components.utils.permissions import admin_access

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedbackEntry:
    """One message and its sender, None once the sender's account is deleted."""

    feedback: UserFeedback
    sender: User | None


async def list_feedback(session: AsyncSession) -> list[FeedbackEntry]:
    """Every message with its sender, unread first and newest first within each group."""
    result = await session.execute(
        select(UserFeedback, User)
        .outerjoin(User, User.id == UserFeedback.user_id)
        .order_by(UserFeedback.read_at.is_not(None), UserFeedback.created_at.desc())
    )
    return [FeedbackEntry(feedback, sender) for feedback, sender in result.tuples()]


async def set_feedback_read(
    session: AsyncSession, feedback_id: UUID, *, read: bool, admin_id: UUID
) -> bool:
    """Mark one message read by ``admin_id``, or unread again. False when no
    message has ``feedback_id``."""
    result = await session.execute(
        update(UserFeedback)
        .where(UserFeedback.id == feedback_id)
        .values(read_at=func.now() if read else None, read_by=admin_id if read else None)
        .returning(UserFeedback.id)
    )
    await session.commit()
    return result.scalar_one_or_none() is not None


async def delete_feedback(session: AsyncSession, feedback_id: UUID) -> bool:
    """Delete one message. False when no message has ``feedback_id``."""
    result = await session.execute(
        delete(UserFeedback).where(UserFeedback.id == feedback_id).returning(UserFeedback.id)
    )
    await session.commit()
    return result.scalar_one_or_none() is not None


def _sender_label(sender: User | None) -> str:
    if sender is None:
        return "Deleted user"
    return f"{sender.name} ({sender.email})"


def _render_feedback_card(
    entry: FeedbackEntry,
    on_toggle: Callable[[UUID, bool], Awaitable[None]],
    on_delete: Callable[[UUID], Awaitable[None]],
) -> None:
    feedback = entry.feedback
    unread = feedback.read_at is None
    with ui.card().classes("w-full gap-2" if unread else "w-full gap-2 opacity-70"):
        with ui.row().classes("w-full items-center gap-2"):
            if unread:
                ui.badge("New", color="primary")
            ui.label(_sender_label(entry.sender)).classes("font-medium")
            ui.label(feedback.created_at.strftime("%Y-%m-%d %H:%M")).classes(
                "text-sm text-gray-500"
            )
            ui.space()
            ui.button(
                "Mark as read" if unread else "Mark as unread",
                icon="mark_email_read" if unread else "mark_email_unread",
                on_click=partial(on_toggle, feedback.id, unread),
            ).props("flat dense no-caps")
            ui.button(
                "Delete", icon="delete", on_click=partial(_confirm_delete, feedback.id, on_delete)
            ).props("flat dense no-caps color=negative").mark(f"delete-feedback-{feedback.id}")
        ui.label(feedback.message).classes("w-full whitespace-pre-wrap break-words")


def _confirm_delete(feedback_id: UUID, on_delete: Callable[[UUID], Awaitable[None]]) -> None:
    async def confirmed() -> None:
        dialog.close()
        await on_delete(feedback_id)

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Delete this message?").classes("text-lg font-bold")
        ui.label("It cannot be restored.").classes("text-sm text-gray-600")
        render_dialog_actions(
            on_confirm=confirmed,
            on_cancel=dialog.close,
            confirm_label="Delete message",
            confirm_props="color=negative",
        )
    dialog.open()


async def render_feedback_panel(
    page_user_id: str, on_unread: Callable[[int], None] | None = None
) -> None:
    """Render the feedback panel for the admin ``page_user_id``, whose marks
    it records. ``on_unread`` hears the unread count after every load."""
    with ui.column().classes("w-full gap-4"):
        ui.markdown("## Feedback")
        summary = ui.label().classes("text-sm text-gray-600")
        entries_container = ui.column().classes("w-full gap-3")

    async def load() -> None:
        access = await admin_access()
        if access is None:
            return
        try:
            async with access.session() as session:
                entries = await list_feedback(session)
        except Exception:
            logger.exception("Error loading feedback")
            entries_container.clear()
            with entries_container:
                render_alert_banner(
                    title="Error",
                    message="Failed to load feedback. Check server logs.",
                    severity="error",
                )
            return
        unread = sum(1 for entry in entries if entry.feedback.read_at is None)
        summary.set_text(f"{unread} unread of {len(entries)}")
        if on_unread is not None:
            on_unread(unread)
        entries_container.clear()
        with entries_container:
            if not entries:
                render_empty_state("No feedback yet.")
            for entry in entries:
                _render_feedback_card(entry, toggle, remove)

    @in_page_client
    async def toggle(feedback_id: UUID, read: bool) -> None:
        access = await admin_access()
        if access is None:
            return
        try:
            async with access.session() as session:
                found = await set_feedback_read(
                    session, feedback_id, read=read, admin_id=UUID(page_user_id)
                )
        except Exception:
            logger.exception("Failed to change the read state of feedback %s", feedback_id)
            ui.notify("Failed to update the message. Please try again.", type="negative")
            return
        if not found:
            ui.notify("This message no longer exists", type="warning")
        await load()

    @in_page_client
    async def remove(feedback_id: UUID) -> None:
        access = await admin_access()
        if access is None:
            return
        try:
            async with access.session() as session:
                found = await delete_feedback(session, feedback_id)
        except Exception:
            logger.exception("Failed to delete feedback %s", feedback_id)
            ui.notify("Failed to delete the message. Please try again.", type="negative")
            return
        if found:
            ui.notify("Message deleted", type="positive")
        else:
            ui.notify("This message no longer exists", type="warning")
        await load()

    await load()
