"""The feedback button and the dialog in which a user writes to the admins."""

import logging
from uuid import UUID

from nicegui import ui
from sqlalchemy import insert
from sqlalchemy.exc import DBAPIError

from openscientist.database.models import UserFeedback
from openscientist.database.models.user_feedback import (
    FEEDBACK_DAILY_LIMIT,
    FEEDBACK_LIMIT_SQLSTATE,
    FEEDBACK_MAX_LENGTH,
)
from openscientist.database.session import thread_safe_user_session
from openscientist.webapp_components.utils.permissions import revalidate_session

logger = logging.getLogger(__name__)


class FeedbackLimitReachedError(Exception):
    """The sender already sent ``FEEDBACK_DAILY_LIMIT`` messages in the last 24 hours."""


async def submit_feedback(user_id: UUID, message: str) -> None:
    """Store ``message`` as feedback from ``user_id`` through the user's own session.

    Raises:
        FeedbackLimitReachedError: The sender reached the daily limit.
    """
    async with thread_safe_user_session(user_id) as session:
        # Inline, so the insert fetches no generated key through a RETURNING
        # the user's session may not read.
        try:
            await session.execute(
                insert(UserFeedback).values(user_id=user_id, message=message).inline()
            )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == FEEDBACK_LIMIT_SQLSTATE:
                raise FeedbackLimitReachedError from error
            raise
        await session.commit()


def render_feedback_button() -> None:
    """A quiet button that opens the dialog in which a user writes to the admins."""
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
        ui.label("Send feedback").classes("text-lg font-bold")
        ui.label(
            "Write anything you would like the team to know, such as something that "
            "went wrong, something that was hard to understand, or an idea."
        ).classes("text-sm text-gray-600")
        message_input = (
            ui.textarea(placeholder="Your message")
            .props(f"outlined autogrow counter maxlength={FEEDBACK_MAX_LENGTH}")
            .classes("w-full")
        )
        ui.label("The administrators see your name with the message.").classes(
            "text-xs text-gray-500"
        )

        async def send() -> None:
            if not send_button.enabled:
                return
            message = (message_input.value or "").strip()
            if not message:
                ui.notify("Please write a few words first", type="warning")
                return
            if len(message) > FEEDBACK_MAX_LENGTH:
                ui.notify(
                    f"Please keep the message under {FEEDBACK_MAX_LENGTH} characters",
                    type="warning",
                )
                return
            send_button.disable()
            try:
                user_info = await revalidate_session()
                if user_info is None:
                    ui.notify("Your session has ended. Please sign in again.", type="warning")
                    return
                await submit_feedback(UUID(user_info["user_id"]), message)
            except FeedbackLimitReachedError:
                ui.notify(
                    f"You have sent {FEEDBACK_DAILY_LIMIT} messages in the last day. "
                    "Please try again tomorrow.",
                    type="warning",
                )
                return
            except Exception:
                logger.exception("Failed to store feedback")
                ui.notify(
                    "Your message could not be sent. Please try again later.", type="negative"
                )
                return
            finally:
                send_button.enable()
            message_input.value = ""
            dialog.close()
            ui.notify("Thank you, your message was sent.", type="positive")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            send_button = ui.button("Send", icon="send", on_click=send).props("color=primary")

    ui.button("Feedback", icon="feedback", on_click=dialog.open).props(
        "flat dense no-caps color=grey-8"
    ).tooltip("Send a message to the team")
