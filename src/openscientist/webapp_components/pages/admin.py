"""Admin page for orphaned job management and container dashboard."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nicegui import app, ui
from sqlalchemy import delete, func, select, update

from openscientist.admin.orphan_jobs import assign_orphaned_job, list_orphaned_jobs
from openscientist.auth.fastapi_routes import _hash_token, generate_review_token
from openscientist.auth.middleware import get_current_user_id, require_admin, require_auth
from openscientist.budget import format_spend
from openscientist.database.models import Job, ReviewToken, Session, User
from openscientist.database.session import get_admin_session
from openscientist.job.types import JobStatus
from openscientist.settings import get_settings
from openscientist.webapp_components.components.badges import render_job_id_badge
from openscientist.webapp_components.components.banners import render_alert_banner
from openscientist.webapp_components.components.common import (
    FONTAWESOME_HEAD_HTML,
    STATUS_COLORS,
    format_uptime,
    make_action_button_slot,
    render_container_status_badge,
    render_dialog_actions,
    render_empty_state,
    render_stat_badges,
    render_user_search,
)
from openscientist.webapp_components.components.job_table import render_job_id_slot
from openscientist.webapp_components.components.navigation import render_navigator
from openscientist.webapp_components.pages.billing import render_billing_panel
from openscientist.webapp_components.pages.settings_panel import render_settings_panel
from openscientist.webapp_components.utils import session_checked_timer
from openscientist.webapp_components.utils.permissions import (
    refuse_stale_admin_session,
    revalidate_admin_session,
)

if TYPE_CHECKING:
    from openscientist.webapp_components.utils.container_dashboard import (
        ContainerInfo,
        DashboardData,
        DashboardTotals,
        JobContainerGroup,
    )

logger = logging.getLogger(__name__)


def _filter_users_for_admin_table(users: list[User], current_user_id: str | None) -> list[User]:
    """Exclude the current user from the admin users table."""
    if not current_user_id:
        return users
    return [user for user in users if str(user.id) != current_user_id]


async def set_user_approval_status(user_id: UUID, is_approved: bool) -> tuple[bool, str]:
    """
    Update a user's approval flag.

    Args:
        user_id: User to update
        is_approved: Desired approval state

    Returns:
        Tuple of (success, message)
    """
    async with get_admin_session() as session:
        stmt = select(User).where(User.id == user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            return False, "User not found"

        # Prevent admins from removing their own approval status.
        current_user_id: str | None = None
        if not is_approved:
            try:
                current_user_id = get_current_user_id()
            except (RuntimeError, AssertionError, AttributeError):
                # Not all call paths/tests have a request-scoped NiceGUI storage context.
                logger.debug("Current user ID unavailable during approval update", exc_info=True)

        if not is_approved and current_user_id == str(user.id):
            return False, "You cannot remove your own approval"

        if bool(user.is_approved) == is_approved:
            state = "approved" if is_approved else "pending"
            return False, f"User is already {state}"

        user.is_approved = is_approved
        await session.commit()

    if is_approved:
        return True, "User approved successfully"
    return True, "User approval removed successfully"


async def set_user_priority_status(user_id: UUID, is_priority: bool) -> tuple[bool, str]:
    """Mark a user's queued jobs as starting ahead of other users' jobs, or not.

    Returns:
        Tuple of (success, message)
    """
    async with get_admin_session() as session:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user:
            return False, "User not found"
        if bool(user.is_priority) == is_priority:
            state = "already" if is_priority else "not"
            return False, f"User is {state} a priority user"
        user.is_priority = is_priority
        await session.commit()

    if is_priority:
        return True, "User is now a priority user"
    return True, "User is no longer a priority user"


@ui.page("/admin")
@require_auth
@require_admin
async def admin_page() -> None:
    """Admin page for managing orphaned jobs and user assignments."""
    # Before the page is sent, so the links go into the first HTML the
    # browser receives and the settings icons render without a flash.
    ui.add_head_html(FONTAWESOME_HEAD_HTML)
    page_user_id = get_current_user_id()
    assert page_user_id is not None
    render_navigator(active_page="admin")

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-6"):
        ui.markdown("# Admin")

        # Tabs for different admin functions
        with ui.tabs().classes("w-full") as tabs:
            orphaned_tab = ui.tab("Orphaned Jobs", icon="work_off")
            users_tab = ui.tab("Users", icon="people")
            review_tokens_tab = ui.tab("Review Tokens", icon="rate_review")
            containers_tab = ui.tab("Containers", icon="dns")
            billing_tab = ui.tab("Billing", icon="payments")
            settings_tab = ui.tab("Settings", icon="tune")

        with ui.tab_panels(tabs, value=users_tab).classes("w-full"):
            # Orphaned Jobs Panel (deferred, filled below)
            orphaned_panel = ui.tab_panel(orphaned_tab)

            # Users Panel (default tab, loaded before the page is sent). Any
            # ui.timer created before this await would make NiceGUI send the
            # page early, so the deferred panels' timers come after it.
            with ui.tab_panel(users_tab):
                await render_users_panel()

            with orphaned_panel:
                ui.timer(0.1, render_orphaned_jobs_panel, once=True)

            # Review Tokens Panel (deferred)
            with ui.tab_panel(review_tokens_tab):
                ui.timer(0.1, render_review_tokens_panel, once=True)

            # Containers Panel (deferred)
            with ui.tab_panel(containers_tab):
                ui.timer(0.1, partial(render_containers_panel, page_user_id), once=True)

            # Billing Panel (deferred)
            with ui.tab_panel(billing_tab):
                ui.timer(0.1, render_billing_panel, once=True)

            # Settings Panel (deferred)
            with ui.tab_panel(settings_tab):
                ui.timer(0.1, render_settings_panel, once=True)


async def render_orphaned_jobs_panel() -> None:
    """Render the orphaned jobs management panel."""

    # Search and filter controls
    with ui.row().classes("w-full gap-4 items-end"):
        search_input = ui.input(
            label="Search jobs",
            placeholder="Job ID or title...",
        ).classes("flex-grow")

        async def refresh_jobs() -> None:
            """Refresh the jobs table."""
            await load_orphaned_jobs(
                container=jobs_container,
                search_query=search_input.value,
            )

        ui.button("Search", icon="search", on_click=refresh_jobs).props("color=primary")

    # Jobs table container
    jobs_container = ui.column().classes("w-full mt-4")

    # Initial load
    await load_orphaned_jobs(container=jobs_container, search_query="")


async def load_orphaned_jobs(container: ui.column, search_query: str = "") -> None:
    """Load and display orphaned jobs."""

    container.clear()

    try:
        # Use admin session to query all orphaned jobs regardless of current user
        async with get_admin_session() as session:
            orphaned_jobs = await list_orphaned_jobs(session, search_query=search_query)

        if not orphaned_jobs:
            with container:
                render_empty_state("No orphaned jobs found.")
            return

        # Display count
        with container:
            ui.label(f"Found {len(orphaned_jobs)} orphaned job(s)").classes("font-bold mb-2")

        # Create table
        columns = [
            {"name": "id", "label": "Job ID", "field": "id", "align": "left"},
            {"name": "title", "label": "Title", "field": "title", "align": "left"},
            {"name": "status", "label": "Status", "field": "status", "align": "left"},
            {
                "name": "created_at",
                "label": "Created",
                "field": "created_at",
                "align": "left",
            },
            {
                "name": "actions",
                "label": "Actions",
                "field": "actions",
                "align": "center",
            },
        ]

        rows = [
            {
                "id": "..." + str(job.id)[-8:],
                "full_id": str(job.id),
                "title": (
                    job.research_question[:50] + ("..." if len(job.research_question) > 50 else "")
                ),
                "status": job.status,
                "created_at": job.created_at.strftime("%Y-%m-%d %H:%M")
                if job.created_at
                else "N/A",
            }
            for job in orphaned_jobs
        ]

        with container:
            table = ui.table(columns=columns, rows=rows, row_key="full_id").classes("w-full")
            # Add job ID column slot with clickable badges
            table.add_slot("body-cell-id", render_job_id_slot(field_name="full_id"))

            table.add_slot(
                "body-cell-actions",
                make_action_button_slot(
                    label="Assign",
                    event_name="assign",
                    icon="person_add",
                ),
            )

            # Handle job ID badge clicks
            table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))

            # Handle assign button clicks
            async def handle_assign(e: Any) -> None:
                job_id = e.args["full_id"]
                await show_assign_dialog(job_id)

            table.on("assign", handle_assign)

    except Exception as e:
        logger.error("Error loading orphaned jobs: %s", e, exc_info=True)
        with container:
            ui.label("Error loading jobs. Check server logs for details.").classes("text-red-500")


async def show_assign_dialog(job_id: str) -> None:
    """Show dialog to assign a job to a user."""
    selected_user: dict[str, Any] | None = None

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Assign Job to User").classes("text-lg font-bold mb-4")
        with ui.row().classes("items-center gap-2 mb-4"):
            ui.label("Job:").classes("text-sm text-gray-600")
            render_job_id_badge(job_id)

        # User search using shared component
        async def on_user_select(user_data: dict[str, Any]) -> None:
            nonlocal selected_user
            selected_user = user_data
            search_input.value = f"{user_data['name']} ({user_data['email']})"
            results_container.clear()

        search_input, results_container = await render_user_search(
            on_select=on_user_select,
            placeholder="Search users by email or name",
        )

        # Action handlers
        async def do_assign() -> None:
            if not selected_user:
                ui.notify("Please select a user", color="negative")
                return
            try:
                user_id = str(selected_user["id"])
            except KeyError:
                ui.notify("Invalid user selection", color="negative")
                return
            await _confirm_assign_orphaned_job(job_id, user_id, dialog=dialog)

        render_dialog_actions(
            on_confirm=do_assign,
            on_cancel=dialog.close,
            confirm_label="Assign",
        )

    dialog.open()


async def _confirm_assign_orphaned_job(
    job_id: str, selected_user_id: str, *, dialog: ui.dialog
) -> None:
    """Assign an orphaned job once the caller's admin session survives a re-check."""
    if not await revalidate_admin_session():
        refuse_stale_admin_session()
        return
    try:
        async with get_admin_session() as session:
            result = await assign_orphaned_job(
                session=session,
                job_id=UUID(job_id),
                user_id=UUID(selected_user_id),
            )
        if result.ok:
            ui.notify("Job assigned successfully", color="positive")
            dialog.close()
            ui.navigate.reload()
            return
        if result.reason == "job_not_found":
            ui.notify("Job not found", color="negative")
            return
        if result.reason == "already_owned":
            ui.notify("Job is no longer orphaned", color="warning")
            return
        if result.reason == "user_not_found":
            ui.notify("Selected user was not found", color="negative")
            return
        ui.notify("Failed to assign job", color="negative")
    except (TypeError, KeyError, ValueError):
        ui.notify("Invalid user selection", color="negative")
    except Exception as e:
        logger.error("Error assigning job: %s", e, exc_info=True)
        ui.notify("Failed to assign job. Check server logs.", color="negative")


async def delete_user(user_id: UUID) -> tuple[bool, str]:
    """
    Delete a user from the system.

    Args:
        user_id: User to delete

    Returns:
        Tuple of (success, message)
    """
    current_user_id: str | None = None
    try:
        current_user_id = get_current_user_id()
    except (RuntimeError, AssertionError, AttributeError):
        logger.debug("Current user ID unavailable during delete", exc_info=True)

    if current_user_id == str(user_id):
        return False, "You cannot delete your own account"

    async with get_admin_session() as session:
        stmt = select(User).where(User.id == user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            return False, "User not found"

        await session.execute(
            update(User)
            .where(
                User.id.in_(
                    select(ReviewToken.redeemed_by_id).where(ReviewToken.created_by_id == user_id)
                )
            )
            .values(is_active=False)
        )
        await session.execute(
            update(ReviewToken).where(ReviewToken.redeemed_by_id == user_id).values(is_active=False)
        )
        await session.delete(user)
        await session.commit()

    return True, "User deleted successfully"


async def revoke_review_token(token_id: UUID) -> tuple[bool, str]:
    """Revoke a review token and disable the reviewer it redeemed."""
    async with get_admin_session() as session:
        result = await session.execute(select(ReviewToken).where(ReviewToken.id == token_id))
        review_token = result.scalar_one_or_none()
        if not review_token:
            return False, "Token not found"
        review_token.is_active = False
        if review_token.redeemed_by_id is not None:
            await session.execute(
                update(User).where(User.id == review_token.redeemed_by_id).values(is_active=False)
            )
            await session.execute(
                delete(Session).where(Session.user_id == review_token.redeemed_by_id)
            )
        await session.commit()
    return True, "Token revoked"


def _get_admin_current_user_id() -> str | None:
    try:
        return get_current_user_id()
    except (RuntimeError, AssertionError, AttributeError):
        logger.debug(
            "Current user ID unavailable while loading users table",
            exc_info=True,
        )
        return None


def _admin_users_columns() -> list[dict[str, str]]:
    return [
        {"name": "name", "label": "Name", "field": "name", "align": "left"},
        {"name": "email", "label": "Email", "field": "email", "align": "left"},
        {"name": "created_at", "label": "Joined", "field": "created_at", "align": "left"},
        {"name": "job_count", "label": "Jobs", "field": "job_count", "align": "center"},
        {
            "name": "approval_status",
            "label": "Approval",
            "field": "approval_status",
            "align": "center",
        },
        {
            "name": "priority_status",
            "label": "Priority",
            "field": "priority_status",
            "align": "center",
        },
        {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
    ]


async def _build_admin_user_rows(session: Any, users: list[User]) -> list[dict[str, object]]:
    # Single aggregated query instead of N+1 per-user queries
    user_ids = [u.id for u in users]
    stmt = (
        select(Job.owner_id, func.count(Job.id))
        .where(Job.owner_id.in_(user_ids))
        .group_by(Job.owner_id)
    )
    result = await session.execute(stmt)
    job_counts: dict[Any, int] = dict(result.all())

    return [
        {
            "id": str(user.id),
            "name": user.name or "N/A",
            "email": user.email,
            "created_at": user.created_at.strftime("%Y-%m-%d") if user.created_at else "N/A",
            "job_count": job_counts.get(user.id, 0),
            "approval_status": "Approved" if user.is_approved else "Pending",
            "is_approved": bool(user.is_approved),
            "priority_status": "Priority" if user.is_priority else "Normal",
            "is_priority": bool(user.is_priority),
        }
        for user in users
    ]


async def _handle_approval_update(
    e: Any, is_approved: bool, reload_users: Callable[[], Awaitable[None]]
) -> None:
    if not await revalidate_admin_session():
        refuse_stale_admin_session()
        return
    user_id = e.args.get("id")
    if not user_id:
        ui.notify("Invalid user selection", color="negative")
        return

    try:
        success, message = await set_user_approval_status(
            UUID(user_id),
            is_approved=is_approved,
        )
        ui.notify(message, color="positive" if success else "info")
        if success:
            await reload_users()
    except ValueError:
        ui.notify("Invalid user selection", color="negative")
    except Exception as ex:
        action = "approve" if is_approved else "unapprove"
        logger.error(
            "Error trying to %s user %s: %s",
            action,
            user_id,
            ex,
            exc_info=True,
        )
        error_message = (
            "Failed to approve user. Check server logs."
            if is_approved
            else "Failed to remove user approval. Check server logs."
        )
        ui.notify(error_message, color="negative")


async def _handle_priority_update(
    e: Any, is_priority: bool, reload_users: Callable[[], Awaitable[None]]
) -> None:
    if not await revalidate_admin_session():
        refuse_stale_admin_session()
        return
    user_id = e.args.get("id")
    if not user_id:
        ui.notify("Invalid user selection", color="negative")
        return
    try:
        success, message = await set_user_priority_status(UUID(user_id), is_priority=is_priority)
    except ValueError:
        ui.notify("Invalid user selection", color="negative")
        return
    except Exception as ex:
        logger.error("Error setting priority of user %s: %s", user_id, ex, exc_info=True)
        ui.notify("Failed to change the user's priority. Check server logs.", color="negative")
        return
    ui.notify(message, color="positive" if success else "info")
    if success:
        await reload_users()


def _render_admin_users_table(
    users_container: ui.column,
    rows: list[dict[str, object]],
    reload_users: Callable[[], Awaitable[None]],
) -> None:
    with users_container:
        users_table = ui.table(columns=_admin_users_columns(), rows=rows, row_key="id").classes(
            "w-full"
        )
        users_table.add_slot(
            "body-cell-approval_status",
            """
            <q-td :props="props">
                <q-badge
                    :color="props.row.is_approved ? 'positive' : 'warning'"
                    :label="props.row.approval_status"
                />
            </q-td>
            """,
        )
        users_table.add_slot(
            "body-cell-priority_status",
            """
            <q-td :props="props">
                <q-badge
                    :color="props.row.is_priority ? 'purple' : 'grey'"
                    :label="props.row.priority_status"
                />
            </q-td>
            """,
        )
        users_table.add_slot(
            "body-cell-actions",
            """
            <q-td :props="props">
                <div class="row items-center gap-1 justify-center flex-wrap">
                    <q-btn
                        v-if="!props.row.is_approved"
                        flat
                        dense
                        size="sm"
                        color="positive"
                        icon="check"
                        @click="$parent.$emit('approve-user', props.row)"
                    >
                        <q-tooltip>Approve user</q-tooltip>
                    </q-btn>
                    <q-btn
                        v-else
                        flat
                        dense
                        size="sm"
                        color="warning"
                        icon="remove_circle"
                        @click="$parent.$emit('unapprove-user', props.row)"
                    >
                        <q-tooltip>Remove approval</q-tooltip>
                    </q-btn>
                    <q-btn
                        v-if="!props.row.is_priority"
                        flat
                        dense
                        size="sm"
                        color="purple"
                        icon="bolt"
                        @click="$parent.$emit('prioritise-user', props.row)"
                    >
                        <q-tooltip>Make priority user</q-tooltip>
                    </q-btn>
                    <q-btn
                        v-else
                        flat
                        dense
                        size="sm"
                        color="grey"
                        icon="bolt"
                        @click="$parent.$emit('deprioritise-user', props.row)"
                    >
                        <q-tooltip>Remove priority</q-tooltip>
                    </q-btn>
                    <q-btn
                        flat
                        dense
                        size="sm"
                        color="negative"
                        icon="delete"
                        @click="$parent.$emit('delete-user', props.row)"
                    >
                        <q-tooltip>Delete user</q-tooltip>
                    </q-btn>
                </div>
            </q-td>
            """,
        )

        async def approve_user(e: Any) -> None:
            await _handle_approval_update(e, is_approved=True, reload_users=reload_users)

        async def unapprove_user(e: Any) -> None:
            await _handle_approval_update(e, is_approved=False, reload_users=reload_users)

        async def prioritise_user(e: Any) -> None:
            await _handle_priority_update(e, is_priority=True, reload_users=reload_users)

        async def deprioritise_user(e: Any) -> None:
            await _handle_priority_update(e, is_priority=False, reload_users=reload_users)

        async def handle_delete_user(e: Any) -> None:
            user_id = e.args.get("id")
            user_name = e.args.get("name", "this user")
            if not user_id:
                ui.notify("Invalid user selection", color="negative")
                return
            _open_delete_user_dialog(
                user_id=user_id,
                user_name=user_name,
                on_deleted=reload_users,
            )

        users_table.on("approve-user", approve_user)
        users_table.on("unapprove-user", unapprove_user)
        users_table.on("prioritise-user", prioritise_user)
        users_table.on("deprioritise-user", deprioritise_user)
        users_table.on("delete-user", handle_delete_user)


def _open_delete_user_dialog(
    *,
    user_id: str,
    user_name: str,
    on_deleted: Callable[[], Awaitable[None]],
) -> None:
    """Open confirmation dialog for user deletion."""

    async def do_delete() -> None:
        await _confirm_delete_user(user_id, dialog=dialog, on_deleted=on_deleted)

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Delete User").classes("text-lg font-bold mb-2")
        ui.label(f'Are you sure you want to delete "{user_name}"?').classes("mb-2")
        ui.label("This will remove the user account. Their jobs will become orphaned.").classes(
            "text-sm text-gray-600 mb-4"
        )
        render_dialog_actions(
            on_confirm=do_delete,
            on_cancel=dialog.close,
            confirm_label="Delete",
            confirm_props="color=negative",
        )
    dialog.open()


async def _confirm_delete_user(
    user_id: str,
    *,
    dialog: ui.dialog,
    on_deleted: Callable[[], Awaitable[None]],
) -> None:
    """Delete a user once the caller's admin session survives a re-check."""
    if not await revalidate_admin_session():
        refuse_stale_admin_session()
        return
    try:
        success, message = await delete_user(UUID(user_id))
        ui.notify(message, color="positive" if success else "negative")
        dialog.close()
        if success:
            await on_deleted()
    except ValueError:
        ui.notify("Invalid user selection", color="negative")
    except Exception as ex:
        logger.error("Error deleting user %s: %s", user_id, ex, exc_info=True)
        ui.notify("Failed to delete user. Check server logs.", color="negative")


async def render_users_panel() -> None:
    """Render the users management panel."""

    with ui.column().classes("w-full gap-4"):
        ui.markdown("## All Users")
        ui.markdown("View all registered users in the system.")

        users_container = ui.column().classes("w-full mt-4")

        async def load_users() -> None:
            """Load all users."""
            users_container.clear()

            try:
                current_user_id = _get_admin_current_user_id()

                async with get_admin_session() as session:
                    stmt = select(User).order_by(User.created_at.desc())
                    result = await session.execute(stmt)
                    users = list(result.scalars().all())
                    users = _filter_users_for_admin_table(users, current_user_id)

                    if not users:
                        empty_message = (
                            "No other users found." if current_user_id else "No users found."
                        )
                        with users_container:
                            render_empty_state(empty_message)
                        return

                    rows = await _build_admin_user_rows(session, users)
                _render_admin_users_table(users_container, rows, load_users)

            except Exception as e:
                logger.error("Error loading users: %s", e, exc_info=True)
                with users_container:
                    ui.label("Error loading users. Check server logs for details.").classes(
                        "text-red-500"
                    )

        await load_users()


async def render_containers_panel(page_user_id: str) -> None:
    """Render the real-time container dashboard panel."""
    from openscientist.webapp_components.utils.container_dashboard import (
        collect_dashboard_data,
    )

    @ui.refreshable
    def render_dashboard(view: DashboardView) -> None:
        _render_dashboard_content(view)

    view = _dashboard_view(await collect_dashboard_data())
    render_dashboard(view)

    async def guarded_refresh() -> None:
        nonlocal view
        new_view = _dashboard_view(await collect_dashboard_data())
        if new_view == view:
            return
        view = new_view
        render_dashboard.refresh(new_view)

    session_checked_timer(3.0, guarded_refresh, page_user_id)


def _job_status_color(status: str) -> str:
    """The badge colour every other page uses for ``status``."""
    try:
        return STATUS_COLORS[JobStatus(status)]
    except ValueError:
        return "gray"


def _render_dashboard_unavailable_state(view: DashboardView) -> bool:
    if not view.docker_available:
        render_alert_banner(
            title="Docker Unavailable",
            message=view.error_text or "Docker daemon is not reachable.",
            severity="warning",
        )
        return True
    if view.error_text:
        render_alert_banner(
            title="Dashboard Error",
            message=view.error_text,
            severity="error",
        )
        return True
    return False


def _render_dashboard_totals(totals: TotalsView) -> None:
    render_stat_badges(
        [
            ("Jobs", totals.jobs, "blue"),
            ("Agents", totals.agents, "green"),
            ("Executors", totals.executors, "orange"),
            ("Memory", totals.memory_label, ""),
            ("CPU", totals.cpu_label, ""),
        ],
        icon_map={
            "Jobs": "work",
            "Agents": "smart_toy",
            "Executors": "code",
            "Memory": "memory",
            "CPU": "speed",
        },
    )


def _render_dashboard_job_groups(view: DashboardView) -> None:
    for card in view.job_cards:
        with ui.card().classes("w-full mb-3"):
            with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                render_job_id_badge(card.job_id)
                ui.label(card.title_label).classes("font-medium text-sm flex-grow")
                ui.badge(card.status, color=_job_status_color(card.status)).classes("px-2")
                ui.badge(card.spend_label, color="green").props("text-color=white").classes(
                    "px-1.5 py-0.5"
                )
                ui.label(card.iteration).classes("text-xs text-gray-500")
                ui.label(card.owner).classes("text-xs text-gray-400")

            ui.separator()

            if card.agent_row:
                _render_container_row(card.agent_row, icon="smart_toy")
            for row in card.executor_rows:
                _render_container_row(row, icon="code")

            if card.agent_row is None and not card.executor_rows:
                ui.label("No containers").classes("text-gray-400 text-sm px-2 py-1")


def _render_dashboard_orphan_containers(view: DashboardView) -> None:
    if not view.orphan_rows:
        return
    key = "admin_orphan_containers_expanded"
    expanded = app.storage.client.get(key, False)
    exp = ui.expansion(
        f"Orphan Containers ({len(view.orphan_rows)})",
        icon="warning",
        value=expanded,
    ).classes("w-full mt-4 border border-orange-200 rounded")
    exp.on_value_change(lambda e: app.storage.client.update({key: e.value}))
    with exp:
        for row in view.orphan_rows:
            _render_container_row(row, icon="help_outline")


def _render_dashboard_content(view: DashboardView) -> None:
    if _render_dashboard_unavailable_state(view):
        return
    _render_dashboard_totals(view.totals)
    if not view.job_cards and not view.orphan_rows:
        render_empty_state("No OpenScientist containers are currently running.")
        return
    _render_dashboard_job_groups(view)
    _render_dashboard_orphan_containers(view)


def _group_title_label(title: str) -> str:
    """The job title as the panel shows it, truncated with an ellipsis."""
    return title[:60] + ("..." if len(title) > 60 else "")


def _total_memory_label(total_memory_mb: float) -> str:
    """The total memory figure as the summary row shows it."""
    return f"{total_memory_mb:.0f} MB"


def _total_cpu_label(total_cpu_percent: float) -> str:
    """The total CPU figure as the summary row shows it."""
    return f"{total_cpu_percent:.1f}%"


def _cpu_percent_label(cpu_percent: float) -> str:
    """The container CPU figure as the row shows it."""
    return f"CPU {cpu_percent:.1f}%"


def _memory_usage_label(memory_mb: float, memory_limit_mb: float) -> str:
    """The memory usage figure as the row shows it."""
    return f"{memory_mb:.0f}/{memory_limit_mb:.0f} MB"


def _memory_bar_fill(usage_percent: float) -> float:
    """The memory bar fill at the resolution the bar renders it."""
    return round(usage_percent, 3)


def _memory_bar_color(usage_percent: float) -> str:
    """The memory bar colour as the row shows it."""
    return "red" if usage_percent > 0.8 else "green"


@dataclass(frozen=True)
class MemoryUsageView:
    """The memory bar of a running container with a limit, at display granularity."""

    fill: float
    colour: str
    label: str


@dataclass(frozen=True)
class ContainerRowView:
    """One container row as the panel displays it, at display granularity."""

    name: str
    status: str
    uptime: str | None
    memory: MemoryUsageView | None
    cpu_label: str | None


@dataclass(frozen=True)
class JobCardView:
    """One job card as the panel displays it, at display granularity."""

    job_id: str
    title_label: str
    status: str
    iteration: str
    owner: str
    spend_label: str
    agent_row: ContainerRowView | None
    executor_rows: tuple[ContainerRowView, ...]


@dataclass(frozen=True)
class TotalsView:
    """The summary row as the panel displays it, at display granularity."""

    jobs: int
    agents: int
    executors: int
    memory_label: str
    cpu_label: str


@dataclass(frozen=True)
class DashboardView:
    """The whole containers panel as the panel displays it, at display granularity."""

    docker_available: bool
    error_text: str | None
    job_cards: tuple[JobCardView, ...]
    orphan_rows: tuple[ContainerRowView, ...]
    totals: TotalsView


def _container_row_view(ci: ContainerInfo) -> ContainerRowView:
    """The displayed values of one container row, formatted as the panel shows them."""
    if ci.status != "running":
        return ContainerRowView(
            name=ci.name, status=ci.status, uptime=None, memory=None, cpu_label=None
        )
    memory: MemoryUsageView | None = None
    if ci.memory_limit_mb > 0:
        usage_percent = ci.memory_mb / ci.memory_limit_mb
        memory = MemoryUsageView(
            fill=_memory_bar_fill(usage_percent),
            colour=_memory_bar_color(usage_percent),
            label=_memory_usage_label(ci.memory_mb, ci.memory_limit_mb),
        )
    return ContainerRowView(
        name=ci.name,
        status=ci.status,
        uptime=format_uptime(ci.uptime_seconds),
        memory=memory,
        cpu_label=_cpu_percent_label(ci.cpu_percent),
    )


def _job_card_view(group: JobContainerGroup) -> JobCardView:
    """The displayed values of one job card, formatted as the panel shows them."""
    return JobCardView(
        job_id=group.job_id,
        title_label=_group_title_label(group.title),
        status=group.status,
        iteration=f"Iteration {group.current_iteration}/{group.max_iterations}",
        owner=group.owner_email,
        spend_label=format_spend(group.spend_usd),
        agent_row=_container_row_view(group.agent_container) if group.agent_container else None,
        executor_rows=tuple(_container_row_view(ec) for ec in group.executor_containers),
    )


def _totals_view(totals: DashboardTotals) -> TotalsView:
    """The displayed values of the summary row, formatted as the panel shows them."""
    return TotalsView(
        jobs=totals.running_jobs,
        agents=totals.agent_containers,
        executors=totals.executor_containers,
        memory_label=_total_memory_label(totals.total_memory_mb),
        cpu_label=_total_cpu_label(totals.total_cpu_percent),
    )


def _dashboard_view(data: DashboardData) -> DashboardView:
    """The displayed values of the whole panel, formatted as the panel shows them."""
    error_text: str | None
    if not data.docker_available:
        error_text = data.error_message or "Docker daemon is not reachable."
    else:
        error_text = data.error_message
    return DashboardView(
        docker_available=data.docker_available,
        error_text=error_text,
        job_cards=tuple(_job_card_view(group) for group in data.job_groups),
        orphan_rows=tuple(_container_row_view(ci) for ci in data.orphan_containers),
        totals=_totals_view(data.totals),
    )


def _render_container_row(row: ContainerRowView, icon: str = "dns") -> None:
    """Render a single container as a compact row."""
    with ui.row().classes("w-full items-center gap-3 px-2 py-1"):
        ui.icon(icon, size="sm").classes("text-gray-500")
        ui.label(row.name).classes("text-xs font-mono text-gray-700").style("min-width: 180px")
        render_container_status_badge(row.status)

        if row.uptime is not None:
            ui.label(row.uptime).classes("text-xs text-gray-500")

            if row.memory is not None:
                with ui.column().classes("gap-0").style("min-width: 100px"):
                    ui.linear_progress(value=row.memory.fill, size="8px").props(
                        f"color={row.memory.colour}"
                    )
                    ui.label(row.memory.label).classes("text-xs text-gray-400")

            if row.cpu_label is not None:
                ui.label(row.cpu_label).classes("text-xs text-gray-500")


# =============================================================================
# Review Tokens Panel
# =============================================================================

_REVIEW_TOKEN_STATUS_SLOT = r"""
<q-td :props="props">
    <q-badge
        :color="{active: 'positive', redeemed: 'blue', expired: 'grey', revoked: 'negative'}[props.row.status] || 'grey'"
        :label="props.row.status"
    />
</q-td>
"""

_REVIEW_TOKEN_ACTIONS_SLOT = r"""
<q-td :props="props">
    <q-btn
        v-if="props.row.can_revoke"
        flat
        dense
        size="sm"
        color="negative"
        icon="block"
        @click="$parent.$emit('revoke', props.row)"
    >
        <q-tooltip>Revoke token</q-tooltip>
    </q-btn>
    <span v-else class="text-grey">-</span>
</q-td>
"""


def _review_token_columns() -> list[dict[str, Any]]:
    return [
        {"name": "label", "label": "Label", "field": "label", "align": "left"},
        {"name": "status", "label": "Status", "field": "status", "align": "center"},
        {"name": "created_at", "label": "Created", "field": "created_at", "align": "left"},
        {"name": "expires_at", "label": "Expires", "field": "expires_at", "align": "left"},
        {"name": "actions", "label": "Actions", "field": "actions", "align": "center"},
    ]


def _review_token_rows(tokens: list[ReviewToken]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for token in tokens:
        rows.append(
            {
                "id": str(token.id),
                "label": token.label,
                "status": token.status,
                "created_at": token.created_at.strftime("%Y-%m-%d %H:%M")
                if token.created_at
                else "N/A",
                "expires_at": token.expires_at.strftime("%Y-%m-%d %H:%M")
                if token.expires_at
                else "Never",
                "can_revoke": token.is_active,
            }
        )
    return rows


async def _create_review_token(label: str, expiry_days: int) -> None:
    """Create a review token once the caller's admin session survives a re-check."""
    if not await revalidate_admin_session():
        refuse_stale_admin_session()
        return
    current_user_id = get_current_user_id()
    if not current_user_id:
        ui.notify("You must be signed in", type="negative")
        return

    expires_at = datetime.now(UTC) + timedelta(days=expiry_days)
    plaintext = generate_review_token()
    token_hash = _hash_token(plaintext)

    try:
        async with get_admin_session() as session:
            review_token = ReviewToken(
                token_hash=token_hash,
                label=label,
                created_by_id=UUID(current_user_id),
                expires_at=expires_at,
            )
            session.add(review_token)
            await session.commit()
    except Exception as exc:
        logger.error("Failed to create review token: %s", exc, exc_info=True)
        ui.notify("Failed to create token. Please try again.", type="negative")
        return

    settings = get_settings()
    magic_link = f"{settings.auth.app_url}/review/{plaintext}"
    await _show_token_created_dialog(magic_link)


async def render_review_tokens_panel() -> None:
    """Render the review tokens management panel."""
    with ui.column().classes("w-full gap-4"):
        ui.markdown("## Review Tokens")
        ui.markdown(
            "Generate magic links for anonymous reviewers. "
            "Each token creates an anonymous account on first use."
        )

        # Create section
        with ui.card().classes("w-full"):
            ui.label("Create Review Token").classes("text-lg font-bold mb-2")
            with ui.row().classes("w-full gap-4 items-center"):
                label_input = ui.input(
                    label="Reviewer Label",
                    placeholder="e.g., Reviewer 1, Nature Review Panel",
                    validation={
                        "Required": lambda v: bool(v and v.strip()),
                        "Max 200 chars": lambda v: len(v) <= 200 if v else True,
                    },
                ).classes("flex-grow")
                label_input.props("outlined dense")

                expiry_input = ui.number(
                    label="Expiry (days)",
                    value=30,
                    min=1,
                    max=365,
                ).classes("w-32")
                expiry_input.props("outlined dense")

                async def create_token() -> None:
                    if not create_button.enabled:
                        return
                    create_button.disable()
                    try:
                        label = (label_input.value or "").strip()
                        if not label:
                            ui.notify("Reviewer label is required", type="negative")
                            return
                        expiry_days = int(expiry_input.value or 30)
                        await _create_review_token(label, expiry_days)
                        label_input.value = ""
                        await load_tokens()
                    finally:
                        create_button.enable()

                create_button = ui.button("Create", icon="add", on_click=create_token).props(
                    "color=primary"
                )

        # Tokens table
        tokens_container = ui.column().classes("w-full mt-4")

        async def load_tokens() -> None:
            tokens_container.clear()
            try:
                async with get_admin_session() as session:
                    stmt = select(ReviewToken).order_by(ReviewToken.created_at.desc())
                    result = await session.execute(stmt)
                    tokens = list(result.scalars().all())

                if not tokens:
                    with tokens_container:
                        render_empty_state("No review tokens yet. Create one to get started.")
                    return

                rows = _review_token_rows(tokens)
                with tokens_container:
                    table = ui.table(
                        columns=_review_token_columns(),
                        rows=rows,
                        row_key="id",
                        pagination=20,
                    ).classes("w-full")
                    table.add_slot("body-cell-status", _REVIEW_TOKEN_STATUS_SLOT)
                    table.add_slot("body-cell-actions", _REVIEW_TOKEN_ACTIONS_SLOT)

                    async def handle_revoke(e: Any) -> None:
                        token_id = e.args.get("id")
                        if not token_id:
                            return
                        _open_revoke_token_dialog(
                            token_id=token_id,
                            token_label=e.args.get("label", ""),
                            on_revoked=load_tokens,
                        )

                    table.on("revoke", handle_revoke)

            except Exception as exc:
                logger.error("Error loading review tokens: %s", exc, exc_info=True)
                with tokens_container:
                    render_alert_banner(
                        title="Error",
                        message="Failed to load review tokens. Check server logs.",
                        severity="error",
                    )

        await load_tokens()


async def _show_token_created_dialog(magic_link: str) -> None:
    """Show one-time dialog with the magic link (shown only once)."""
    with ui.dialog().props("persistent") as dialog, ui.card().classes("w-full max-w-lg"):
        ui.label("Review Token Created").classes("text-lg font-bold mb-2")
        with ui.row().classes("w-full bg-amber-100 border-l-4 border-amber-500 p-3 mb-4"):
            ui.icon("warning", color="amber-700").classes("mr-2")
            ui.label("Copy this link now. You won't be able to see it again!").classes(
                "text-amber-800"
            )
        with ui.row().classes("w-full gap-2 items-center"):
            link_display = ui.input(value=magic_link).classes("flex-grow")
            link_display.props("readonly outlined dense")

            async def copy_link() -> None:
                await ui.run_javascript(f"navigator.clipboard.writeText({magic_link!r})")
                ui.notify("Link copied to clipboard", type="positive")

            ui.button(icon="content_copy", on_click=copy_link).props("flat color=primary").tooltip(
                "Copy to clipboard"
            )
        ui.markdown("Send this link to the reviewer. It opens a sign-in page for them.").classes(
            "mt-4 text-sm text-gray-600"
        )
        with ui.row().classes("w-full justify-end mt-4"):
            ui.button("Done", on_click=dialog.close).props("color=primary")
    dialog.open()


async def _confirm_revoke_review_token(
    token_id: str,
    *,
    dialog: ui.dialog,
    on_revoked: Callable[[], Awaitable[None]],
) -> None:
    """Revoke a review token once the caller's admin session survives a re-check."""
    if not await revalidate_admin_session():
        refuse_stale_admin_session()
        return
    try:
        success, message = await revoke_review_token(UUID(token_id))
        ui.notify(message, type="positive" if success else "negative")
        dialog.close()
        if success:
            await on_revoked()
    except Exception as exc:
        logger.error("Failed to revoke review token: %s", exc, exc_info=True)
        ui.notify("Failed to revoke token. Please try again.", type="negative")


def _open_revoke_token_dialog(
    *,
    token_id: str,
    token_label: str,
    on_revoked: Callable[[], Awaitable[None]],
) -> None:
    """Open confirmation dialog for token revocation."""

    async def do_revoke() -> None:
        await _confirm_revoke_review_token(token_id, dialog=dialog, on_revoked=on_revoked)

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Revoke Review Token").classes("text-lg font-bold mb-2")
        ui.label(f'Are you sure you want to revoke the token "{token_label}"?').classes("mb-2")
        ui.label("The link stops working and the reviewer's open sessions end.").classes(
            "text-sm text-gray-600 mb-4"
        )
        render_dialog_actions(
            on_confirm=do_revoke,
            on_cancel=dialog.close,
            confirm_label="Revoke",
            confirm_props="color=negative",
        )
    dialog.open()
