"""Share and delete dialogs for jobs."""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nicegui import ui
from sqlalchemy import select

from openscientist.auth import can_current_user_start_jobs, get_current_user_id
from openscientist.database.models import Job, JobShare, User
from openscientist.database.session import get_admin_session
from openscientist.share_service import find_share_target_by_email
from openscientist.webapp_components.components.badges import render_job_id_badge
from openscientist.webapp_components.components.common import render_dialog_actions

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from openscientist.job_manager import JobManager


async def _load_job_shares(job_id: str) -> list[tuple[JobShare, User]]:
    """Load current share rows for the target job."""
    async with get_admin_session() as session:
        result = await session.execute(
            select(JobShare, User)
            .join(User, JobShare.shared_with_user_id == User.id)
            .where(JobShare.job_id == UUID(job_id))
            .order_by(User.email)
        )
    return list(result.tuples().all())


def _render_share_rows(
    shares_container: ui.element,
    shares: list[tuple[JobShare, User]],
    on_revoke: Callable[[str], Awaitable[None]],
) -> None:
    """Render current share list cards."""
    shares_container.clear()
    with shares_container:
        if not shares:
            ui.label("No shares yet").classes("text-gray-500 italic")
            return
        ui.label("Current Shares").classes("text-subtitle2 font-bold mb-2")
        for share, target_user in shares:
            with (
                ui.card().classes("w-full p-2"),
                ui.row().classes("items-center justify-between w-full"),
            ):
                with ui.column():
                    ui.label(target_user.name).classes("font-bold")
                    ui.label(target_user.email).classes("text-sm text-gray-600")
                with ui.row().classes("items-center gap-2"):
                    ui.badge(share.permission_level, color="blue")
                    ui.button(icon="delete", on_click=lambda s=share: on_revoke(str(s.id))).props(
                        "flat dense color=red"
                    )


async def _revoke_share(job_id: str, share_id: str, current_user_id: str) -> tuple[bool, str]:
    """Revoke a share after checking ownership."""
    async with get_admin_session() as session:
        job_obj = await session.get(Job, UUID(job_id))
        if not job_obj or str(job_obj.owner_id) != current_user_id:
            return False, "Only the job owner can revoke shares"
        result = await session.execute(select(JobShare).where(JobShare.id == UUID(share_id)))
        share = result.scalar_one_or_none()
        if not share:
            return False, "Share not found"
        await session.delete(share)
        await session.commit()
    return True, "Share revoked successfully"


async def _share_with_user(
    job_id: str,
    email: str,
    permission_level: str,
    current_user_id: str,
) -> tuple[bool, str]:
    """Create or update share with target user email."""
    async with get_admin_session() as session:
        job_obj = await session.get(Job, UUID(job_id))
        if not job_obj or str(job_obj.owner_id) != current_user_id:
            return False, "Only the job owner can share this job"
        target = await session.execute(select(User).where(User.email == email))
        target_user = target.scalar_one_or_none()
        if not target_user:
            return False, f"User '{email}' not found"
        if str(target_user.id) == current_user_id:
            return False, "Cannot share with yourself"

        existing = await session.execute(
            select(JobShare).where(
                JobShare.job_id == UUID(job_id),
                JobShare.shared_with_user_id == target_user.id,
            )
        )
        existing_share = existing.scalar_one_or_none()
        if existing_share:
            existing_share.permission_level = permission_level
        else:
            session.add(
                JobShare(
                    job_id=UUID(job_id),
                    shared_with_user_id=target_user.id,
                    permission_level=permission_level,
                )
            )
        await session.commit()
    return True, f"Shared with {email}"


async def _load_job_public_state(job_id: str) -> tuple[bool, str | None]:
    """The job's public flag and owner id, read through the admin session."""
    async with get_admin_session() as session:
        job_obj = await session.get(Job, UUID(job_id))
    if job_obj is None:
        return False, None
    return bool(job_obj.is_public), str(job_obj.owner_id) if job_obj.owner_id else None


async def _set_job_public_from_ui(
    job_id: str, is_public: bool, current_user_id: str
) -> tuple[bool, str]:
    """Change a job's public visibility after checking ownership."""
    async with get_admin_session() as session:
        job_obj = await session.get(Job, UUID(job_id))
        if not job_obj or str(job_obj.owner_id) != current_user_id:
            return False, "Only the job owner can change its visibility"
        job_obj.is_public = is_public
        await session.commit()
    if is_public:
        return True, "Any signed-in user with the link can now view this job"
    return True, "This job is private again"


def _render_selected_share_user(
    selected_user_container: ui.element,
    email: str,
    name: str,
    clear_selection: Callable[[], None],
) -> None:
    """Render selected user card above permission row."""
    selected_user_container.clear()
    with selected_user_container, ui.row().classes("items-center gap-2 p-2 bg-blue-50 rounded"):
        ui.icon("person", color="primary")
        with ui.column().classes("gap-0"):
            ui.label(name).classes("font-bold text-sm")
            ui.label(email).classes("text-xs text-gray-600")
        ui.button(icon="close", on_click=clear_selection).props("flat dense round size=xs")


class _ShareDialogController:
    """Controller for share dialog state and actions."""

    def __init__(
        self,
        job_id: str,
        shares_container: ui.element,
        public_switch: ui.switch,
        email_input: ui.input,
        selected_user_container: ui.element,
        share_action_row: ui.element,
        permission_select: ui.select,
    ) -> None:
        self.job_id = job_id
        self.shares_container = shares_container
        self.public_switch = public_switch
        self._syncing_public_switch = False
        self.email_input = email_input
        self.selected_user_container = selected_user_container
        self.share_action_row = share_action_row
        self.permission_select = permission_select
        self.selected_user_state: dict[str, str | None] = {"email": None, "name": None}

    async def refresh_public_switch(self) -> None:
        """Show the job's stored visibility to the owner, or hide it otherwise."""
        try:
            is_public, owner_id = await _load_job_public_state(self.job_id)
        except Exception as exc:
            logger.error("Failed to load job visibility: %s", exc, exc_info=True)
            return
        current_user_id = get_current_user_id()
        self.public_switch.visible = owner_id is not None and owner_id == current_user_id
        self._syncing_public_switch = True
        try:
            self.public_switch.value = is_public
        finally:
            self._syncing_public_switch = False

    async def on_public_change(self, event: Any) -> None:
        """Persist a visibility change made with the switch."""
        if self._syncing_public_switch:
            return
        try:
            current_user_id = get_current_user_id()
            if not current_user_id:
                ui.notify("You must be signed in to change visibility", type="negative")
                return
            success, message = await _set_job_public_from_ui(
                self.job_id, bool(event.value), current_user_id
            )
            ui.notify(message, type="positive" if success else "negative")
        except Exception as exc:
            logger.error("Failed to change job visibility: %s", exc, exc_info=True)
            ui.notify("Error changing visibility", type="negative")
        await self.refresh_public_switch()

    def clear_selection(self) -> None:
        """Clear selected share target and show the email input again."""
        self.selected_user_state["email"] = None
        self.selected_user_state["name"] = None
        self.selected_user_container.clear()
        self.share_action_row.classes(add="hidden")
        self.email_input.visible = True
        self.email_input.value = ""

    def select_user(self, email: str, name: str) -> None:
        """Select the looked-up user and reveal share controls."""
        self.selected_user_state["email"] = email
        self.selected_user_state["name"] = name
        self.email_input.visible = False
        _render_selected_share_user(self.selected_user_container, email, name, self.clear_selection)
        self.share_action_row.classes(remove="hidden")

    async def refresh_shares(self) -> None:
        """Reload current shares list and visibility from the database."""
        await self.refresh_public_switch()
        try:
            shares = await _load_job_shares(self.job_id)
            _render_share_rows(self.shares_container, shares, self.revoke_share)
        except Exception as exc:
            logger.error("Failed to load shares: %s", exc, exc_info=True)
            self.shares_container.clear()
            with self.shares_container:
                ui.label("Failed to load shares").classes("text-red-600")

    async def revoke_share(self, share_id: str) -> None:
        """Revoke share and refresh list on success."""
        try:
            current_user_id = get_current_user_id()
            if not current_user_id:
                ui.notify("You must be signed in to revoke shares", type="negative")
                return
            success, message = await _revoke_share(self.job_id, share_id, current_user_id)
            ui.notify(message, type="positive" if success else "negative")
            if success:
                await self.refresh_shares()
        except Exception as exc:
            logger.error("Failed to revoke share: %s", exc, exc_info=True)
            ui.notify("Error revoking share", type="negative")

    async def add_user(self) -> None:
        """Look up the typed email exactly and reveal the share controls for a match."""
        if not can_current_user_start_jobs():
            ui.notify("Your account is pending administrator approval", type="negative")
            return
        email = (self.email_input.value or "").strip()
        try:
            target = await find_share_target_by_email(
                email,
                admin_session_factory=get_admin_session,
            )
        except Exception as exc:
            logger.error("Failed to look up share target: %s", exc, exc_info=True)
            ui.notify("Error looking up user", type="negative")
            return
        if target is None:
            ui.notify("No account uses that email", type="warning")
            return
        self.select_user(target.email, target.name or target.email)

    async def do_share(self) -> None:
        """Create/update share for selected user and refresh list."""
        email = self.selected_user_state["email"]
        if not email:
            ui.notify("Select a user first", type="warning")
            return
        try:
            current_user_id = get_current_user_id()
            if not current_user_id:
                ui.notify("You must be signed in to share jobs", type="negative")
                return
            success, message = await _share_with_user(
                self.job_id,
                email,
                self.permission_select.value,
                current_user_id,
            )
            ui.notify(message, type="positive" if success else "negative")
            if success:
                self.clear_selection()
                await self.refresh_shares()
        except Exception as exc:
            logger.error("Failed to share job: %s", exc, exc_info=True)
            ui.notify("Error sharing job", type="negative")


def render_share_dialog(job_id: str) -> ui.dialog:
    """Create and return a share dialog for a job."""
    with ui.dialog() as dialog, ui.card().classes("w-[600px]"):
        with ui.row().classes("items-center gap-2 mb-4"):
            ui.label("Share Job").classes("text-h6")
            render_job_id_badge(job_id)

        with ui.column().classes("w-full gap-0 mb-2"):
            public_switch = ui.switch("Anyone signed in can view this job")
            ui.label(
                "Readers see the report, findings and job files, but not your uploaded "
                "inputs, chat or costs. Outputs printed from the data become visible."
            ).classes("text-xs text-gray-600")

        shares_container = ui.column().classes("w-full mb-4")
        ui.separator()
        ui.label("Add New Share").classes("text-subtitle2 font-bold mb-2")
        with ui.row().classes("w-full items-end gap-2"):
            email_input = ui.input("Email", placeholder="user@example.com").classes("w-full")
            add_button = ui.button("Add", icon="person_search").props("color=primary")
        selected_user_container = ui.column().classes("w-full")
        share_action_row = ui.row().classes("w-full gap-4 items-end hidden")

        with share_action_row:
            permission_select = ui.select(
                ["view", "edit"], value="view", label="Permission Level"
            ).classes("min-w-32")
            permission_select.props("outlined dense")

        controller = _ShareDialogController(
            job_id=job_id,
            shares_container=shares_container,
            public_switch=public_switch,
            email_input=email_input,
            selected_user_container=selected_user_container,
            share_action_row=share_action_row,
            permission_select=permission_select,
        )
        add_button.on_click(controller.add_user)
        public_switch.on_value_change(controller.on_public_change)
        with share_action_row:
            ui.button("Share", icon="person_add", on_click=controller.do_share).props(
                "color=primary"
            )

        with ui.row().classes("w-full justify-end gap-2 mt-4"):
            ui.button("Close", on_click=dialog.close)
        dialog.on("open", controller.refresh_shares)

    return dialog


def render_delete_dialog(
    job_id: str,
    job_manager: "JobManager",
    on_deleted: Callable[[], None | Awaitable[None]] | None = None,
) -> ui.dialog:
    """Create and return a delete confirmation dialog for a job.

    Active (running or queued) jobs are cancelled before deletion.
    """
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        content_container = ui.column().classes("w-full")

        async def render_dialog_content() -> None:
            job_info = await job_manager.get_job(job_id)
            is_active = job_info and job_info.status in job_manager.ACTIVE_STATUSES
            content_container.clear()

            with content_container:
                ui.label("Delete Job").classes("text-h6 font-bold")
                with ui.row().classes("items-center gap-1 my-2"):
                    ui.label("Are you sure you want to delete job")
                    render_job_id_badge(job_id)
                    ui.label("?")

                if is_active:
                    ui.label(
                        "This job is currently active and will be cancelled before deletion."
                    ).classes("text-caption text-orange-600 mb-1")

                ui.label(
                    "This action cannot be undone. All job data and findings will be permanently deleted."
                ).classes("text-caption text-red-600")

        async def on_confirm() -> None:
            dialog.close()
            try:
                # Verify caller is the job owner
                current_user_id = get_current_user_id()
                if not current_user_id:
                    ui.notify("You must be signed in to delete jobs", type="negative")
                    return
                async with get_admin_session() as session:
                    job_obj = await session.get(Job, UUID(job_id))
                if not job_obj or str(job_obj.owner_id) != current_user_id:
                    ui.notify("Only the job owner can delete this job", type="negative")
                    return

                job_info = await job_manager.get_job(job_id)
                if job_info and job_info.status in job_manager.ACTIVE_STATUSES:
                    await job_manager.cancel_job(job_id)

                await job_manager.delete_job(job_id)
                short_id = job_id[-8:] if len(job_id) > 8 else job_id
                ui.notify(f"Job {short_id} deleted successfully", type="positive")
                if on_deleted:
                    result = on_deleted()
                    if result is not None:
                        await result
            except ValueError:
                ui.notify(
                    "Invalid job or job cannot be deleted in its current state.", type="negative"
                )
            except Exception as e:
                logger.error("Failed to delete job %s: %s", job_id, e, exc_info=True)
                ui.notify("Failed to delete job. Please try again.", type="negative")

        render_dialog_actions(
            on_confirm=on_confirm,
            on_cancel=dialog.close,
            confirm_label="Delete",
            confirm_props="color=negative",
        )

        # Reload on every open so the dialog shows the current job status
        dialog.on("show", render_dialog_content)

    return dialog
