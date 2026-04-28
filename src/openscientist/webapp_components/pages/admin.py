"""Admin page for orphaned job management and container dashboard."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from nicegui import app, ui
from sqlalchemy import func, select

from openscientist.admin.orphan_jobs import assign_orphaned_job, list_orphaned_jobs
from openscientist.auth.middleware import get_current_user_id, require_admin, require_auth
from openscientist.database.models import Job, ReviewToken, User
from openscientist.database.session import get_admin_session
from openscientist.webapp_components.pages.billing import render_billing_panel
from openscientist.webapp_components.ui_components import (
    format_uptime,
    make_action_button_slot,
    render_alert_banner,
    render_container_status_badge,
    render_dialog_actions,
    render_empty_state,
    render_job_id_badge,
    render_job_id_slot,
    render_navigator,
    render_stat_badges,
    render_user_search,
)
from openscientist.webapp_components.utils import guard_client, setup_timer_cleanup

if TYPE_CHECKING:
    from openscientist.webapp_components.utils.container_dashboard import (
        ContainerInfo,
        DashboardData,
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


@ui.page("/admin")
@require_auth
@require_admin
async def admin_page() -> None:
    """Admin page for managing orphaned jobs and user assignments."""
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

        with ui.tab_panels(tabs, value=users_tab).classes("w-full"):
            # Orphaned Jobs Panel (deferred — not the default tab)
            with ui.tab_panel(orphaned_tab):
                ui.timer(0.1, render_orphaned_jobs_panel, once=True)

            # Users Panel (default tab — load eagerly)
            with ui.tab_panel(users_tab):
                await render_users_panel()

            # Review Tokens Panel (deferred)
            with ui.tab_panel(review_tokens_tab):
                ui.timer(0.1, render_review_tokens_panel, once=True)

            # Containers Panel (deferred)
            with ui.tab_panel(containers_tab):
                ui.timer(0.1, render_containers_panel, once=True)

            # Billing Panel (deferred)
            with ui.tab_panel(billing_tab):
                ui.timer(0.1, render_billing_panel, once=True)


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
                selected_user_id = UUID(str(selected_user["id"]))
                async with get_admin_session() as session:
                    result = await assign_orphaned_job(
                        session=session,
                        job_id=UUID(job_id),
                        user_id=selected_user_id,
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

        render_dialog_actions(
            on_confirm=do_assign,
            on_cancel=dialog.close,
            confirm_label="Assign",
        )

    dialog.open()


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

        await session.delete(user)
        await session.commit()

    return True, "User deleted successfully"


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
        }
        for user in users
    ]


async def _handle_approval_update(
    e: Any, is_approved: bool, reload_users: Callable[[], Awaitable[None]]
) -> None:
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
            "body-cell-actions",
            """
            <q-td :props="props">
                <div class="q-gutter-xs">
                    <q-btn
                        v-if="!props.row.is_approved"
                        size="sm"
                        color="positive"
                        icon="check"
                        label="Approve"
                        @click="$parent.$emit('approve-user', props.row)"
                    />
                    <q-btn
                        v-else
                        size="sm"
                        color="warning"
                        icon="remove_circle"
                        label="Unapprove"
                        @click="$parent.$emit('unapprove-user', props.row)"
                    />
                    <q-btn
                        size="sm"
                        color="negative"
                        icon="delete"
                        label="Delete"
                        @click="$parent.$emit('delete-user', props.row)"
                    />
                </div>
            </q-td>
            """,
        )

        async def approve_user(e: Any) -> None:
            await _handle_approval_update(e, is_approved=True, reload_users=reload_users)

        async def unapprove_user(e: Any) -> None:
            await _handle_approval_update(e, is_approved=False, reload_users=reload_users)

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
        users_table.on("delete-user", handle_delete_user)


def _open_delete_user_dialog(
    *,
    user_id: str,
    user_name: str,
    on_deleted: Callable[[], Awaitable[None]],
) -> None:
    """Open confirmation dialog for user deletion."""

    async def do_delete() -> None:
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


async def render_containers_panel() -> None:
    """Render the real-time container dashboard panel."""
    from openscientist.webapp_components.utils.container_dashboard import (
        collect_dashboard_data,
    )

    _active_timers = setup_timer_cleanup()

    @ui.refreshable
    def render_dashboard(data: DashboardData) -> None:
        _render_dashboard_content(data)

    data = await collect_dashboard_data()
    render_dashboard(data)

    @guard_client
    async def guarded_refresh() -> None:
        new_data = await collect_dashboard_data()
        render_dashboard.refresh(new_data)

    timer = ui.timer(3.0, guarded_refresh)
    _active_timers.append(timer)


def _job_status_color(status: str) -> str:
    """Map a job status string to a Quasar badge color."""
    return {
        "running": "yellow",
        "queued": "blue",
        "pending": "grey",
        "completed": "green",
        "failed": "red",
        "cancelled": "grey",
    }.get(status, "grey")


def _render_dashboard_unavailable_state(data: DashboardData) -> bool:
    if not data.docker_available:
        render_alert_banner(
            title="Docker Unavailable",
            message=data.error_message or "Docker daemon is not reachable.",
            severity="warning",
        )
        return True
    if data.error_message:
        render_alert_banner(
            title="Dashboard Error",
            message=data.error_message,
            severity="error",
        )
        return True
    return False


def _render_dashboard_totals(data: DashboardData) -> None:
    totals = data.totals
    render_stat_badges(
        [
            ("Jobs", totals.running_jobs, "blue"),
            ("Agents", totals.agent_containers, "green"),
            ("Executors", totals.executor_containers, "orange"),
            ("Memory", f"{totals.total_memory_mb:.0f} MB", ""),
            ("CPU", f"{totals.total_cpu_percent:.1f}%", ""),
        ],
        icon_map={
            "Jobs": "work",
            "Agents": "smart_toy",
            "Executors": "code",
            "Memory": "memory",
            "CPU": "speed",
        },
    )


def _render_dashboard_job_groups(data: DashboardData) -> None:
    for group in data.job_groups:
        with ui.card().classes("w-full mb-3"):
            with ui.row().classes("w-full items-center gap-3 flex-wrap"):
                render_job_id_badge(group.job_id)
                ui.label(group.title[:60] + ("..." if len(group.title) > 60 else "")).classes(
                    "font-medium text-sm flex-grow"
                )
                ui.badge(group.status, color=_job_status_color(group.status)).classes("px-2")
                ui.label(f"Iteration {group.current_iteration}/{group.max_iterations}").classes(
                    "text-xs text-gray-500"
                )
                ui.label(group.owner_email).classes("text-xs text-gray-400")

            ui.separator()

            if group.agent_container:
                _render_container_row(group.agent_container, icon="smart_toy")
            for ec in group.executor_containers:
                _render_container_row(ec, icon="code")

            if not group.agent_container and not group.executor_containers:
                ui.label("No containers").classes("text-gray-400 text-sm px-2 py-1")


def _render_dashboard_orphan_containers(data: DashboardData) -> None:
    if not data.orphan_containers:
        return
    key = "admin_orphan_containers_expanded"
    expanded = app.storage.client.get(key, False)
    exp = ui.expansion(
        f"Orphan Containers ({len(data.orphan_containers)})",
        icon="warning",
        value=expanded,
    ).classes("w-full mt-4 border border-orange-200 rounded")
    exp.on_value_change(lambda e: app.storage.client.update({key: e.value}))
    with exp:
        for oc in data.orphan_containers:
            _render_container_row(oc, icon="help_outline")


def _render_dashboard_content(data: DashboardData) -> None:
    if _render_dashboard_unavailable_state(data):
        return
    _render_dashboard_totals(data)
    if not data.job_groups and not data.orphan_containers:
        render_empty_state("No OpenScientist containers are currently running.")
        return
    _render_dashboard_job_groups(data)
    _render_dashboard_orphan_containers(data)


def _render_container_row(ci: ContainerInfo, icon: str = "dns") -> None:
    """Render a single container as a compact row."""
    with ui.row().classes("w-full items-center gap-3 px-2 py-1"):
        ui.icon(icon, size="sm").classes("text-gray-500")
        ui.label(ci.name).classes("text-xs font-mono text-gray-700").style("min-width: 180px")
        render_container_status_badge(ci.status)

        if ci.status == "running":
            ui.label(format_uptime(ci.uptime_seconds)).classes("text-xs text-gray-500")

            # Memory bar
            if ci.memory_limit_mb > 0:
                pct = ci.memory_mb / ci.memory_limit_mb
                with ui.column().classes("gap-0").style("min-width: 100px"):
                    ui.linear_progress(value=pct, size="8px").props(
                        f"color={'red' if pct > 0.8 else 'green'}"
                    )
                    ui.label(f"{ci.memory_mb:.0f}/{ci.memory_limit_mb:.0f} MB").classes(
                        "text-xs text-gray-400"
                    )

            ui.label(f"CPU {ci.cpu_percent:.1f}%").classes("text-xs text-gray-500")


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
        color="negative"
        icon="block"
        label="Revoke"
        @click="$parent.$emit('revoke', props.row)"
    />
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
                "can_revoke": token.is_active and not token.is_redeemed,
            }
        )
    return rows


async def render_review_tokens_panel() -> None:
    """Render the review tokens management panel."""
    from openscientist.auth.fastapi_routes import _hash_token, generate_review_token
    from openscientist.settings import get_settings

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
                    label = (label_input.value or "").strip()
                    if not label:
                        ui.notify("Reviewer label is required", type="negative")
                        return

                    current_user_id = get_current_user_id()
                    if not current_user_id:
                        ui.notify("You must be signed in", type="negative")
                        return

                    expiry_days = int(expiry_input.value or 30)
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
                    label_input.value = ""
                    await load_tokens()

                ui.button("Create", icon="add", on_click=create_token).props("color=primary")

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
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
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
        ui.markdown(
            "Send this link to the reviewer. They will be logged in automatically "
            "when they click it."
        ).classes("mt-4 text-sm text-gray-600")
        with ui.row().classes("w-full justify-end mt-4"):
            ui.button("Done", on_click=dialog.close).props("color=primary")
    dialog.open()


def _open_revoke_token_dialog(
    *,
    token_id: str,
    token_label: str,
    on_revoked: Callable[[], Awaitable[None]],
) -> None:
    """Open confirmation dialog for token revocation."""

    async def do_revoke() -> None:
        try:
            async with get_admin_session() as session:
                result = await session.execute(
                    select(ReviewToken).where(ReviewToken.id == UUID(token_id))
                )
                review_token = result.scalar_one_or_none()
                if not review_token:
                    ui.notify("Token not found", type="negative")
                    dialog.close()
                    return
                review_token.is_active = False
                await session.commit()
            ui.notify("Token revoked", type="positive")
            dialog.close()
            await on_revoked()
        except Exception as exc:
            logger.error("Failed to revoke review token: %s", exc, exc_info=True)
            ui.notify("Failed to revoke token. Please try again.", type="negative")

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Revoke Review Token").classes("text-lg font-bold mb-2")
        ui.label(f'Are you sure you want to revoke the token "{token_label}"?').classes("mb-2")
        ui.label("The reviewer will no longer be able to use this link to log in.").classes(
            "text-sm text-gray-600 mb-4"
        )
        render_dialog_actions(
            on_confirm=do_revoke,
            on_cancel=dialog.close,
            confirm_label="Revoke",
            confirm_props="color=negative",
        )
    dialog.open()
