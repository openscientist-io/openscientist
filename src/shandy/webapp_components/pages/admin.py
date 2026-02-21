"""Admin page for orphaned job management and container dashboard."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from nicegui import app, ui
from sqlalchemy import String, select

from shandy.auth.middleware import require_admin, require_auth
from shandy.database.models import Job, User
from shandy.database.session import get_admin_session
from shandy.webapp_components.ui_components import (
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
from shandy.webapp_components.utils import guard_client, setup_timer_cleanup

if TYPE_CHECKING:
    from shandy.webapp_components.utils.container_dashboard import ContainerInfo

logger = logging.getLogger(__name__)


@ui.page("/admin")
@require_auth
@require_admin
async def admin_page():
    """Admin page for managing orphaned jobs and user assignments."""
    render_navigator(active_page="admin")

    with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-6"):
        ui.markdown("# Admin - Orphaned Jobs")
        ui.markdown(
            "Manage jobs that were imported from file-based storage without an owner. "
            "Assign these jobs to users or allow users to claim them."
        )

        # Tabs for different admin functions
        with ui.tabs().classes("w-full") as tabs:
            orphaned_tab = ui.tab("Orphaned Jobs", icon="work_off")
            users_tab = ui.tab("Users", icon="people")
            legacy_user_tab = ui.tab("Legacy User", icon="person_add")
            containers_tab = ui.tab("Containers", icon="dns")

        with ui.tab_panels(tabs, value=orphaned_tab).classes("w-full"):
            # Orphaned Jobs Panel
            with ui.tab_panel(orphaned_tab):
                await render_orphaned_jobs_panel()

            # Users Panel
            with ui.tab_panel(users_tab):
                await render_users_panel()

            # Legacy User Panel
            with ui.tab_panel(legacy_user_tab):
                await render_legacy_user_panel()

            # Containers Panel
            with ui.tab_panel(containers_tab):
                await render_containers_panel()


async def render_orphaned_jobs_panel():
    """Render the orphaned jobs management panel."""

    # Search and filter controls
    with ui.row().classes("w-full gap-4 items-end"):
        search_input = ui.input(
            label="Search jobs",
            placeholder="Job ID or title...",
        ).classes("flex-grow")

        async def refresh_jobs():
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


async def load_orphaned_jobs(container: ui.column, search_query: str = ""):
    """Load and display orphaned jobs."""

    container.clear()

    try:
        # Use admin session to query all orphaned jobs regardless of current user
        async with get_admin_session() as session:
            # Query orphaned jobs (owner_id IS NULL)
            stmt = select(Job).where(Job.owner_id.is_(None))

            # Add search filter if provided
            if search_query:
                stmt = stmt.where(
                    Job.title.ilike(f"%{search_query}%")
                    | Job.id.cast(String).ilike(f"%{search_query}%")
                )

            stmt = stmt.order_by(Job.created_at.desc())

            result = await session.execute(stmt)
            orphaned_jobs = result.scalars().all()

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

        rows = []
        for job in orphaned_jobs:
            rows.append(
                {
                    "id": str(job.id)[:8] + "...",
                    "full_id": str(job.id),
                    "title": job.title[:50] + ("..." if len(job.title) > 50 else ""),
                    "status": job.status,
                    "created_at": (
                        job.created_at.strftime("%Y-%m-%d %H:%M") if job.created_at else "N/A"
                    ),
                }
            )

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
                    row_id_field="full_id",
                ),
            )

            # Handle job ID badge clicks
            table.on("view-job", lambda e: ui.navigate.to(f"/job/{e.args}"))

            # Handle assign button clicks
            async def handle_assign(e):
                job_id = e.args["full_id"]
                await show_assign_dialog(job_id)

            table.on("assign", handle_assign)

    except Exception as e:
        logger.error("Error loading orphaned jobs: %s", e, exc_info=True)
        with container:
            ui.label("Error loading jobs. Check server logs for details.").classes("text-red-500")


async def show_assign_dialog(job_id: str):
    """Show dialog to assign a job to a user."""
    selected_user: dict | None = None

    with ui.dialog() as dialog, ui.card().classes("w-96"):
        ui.label("Assign Job to User").classes("text-lg font-bold mb-4")
        with ui.row().classes("items-center gap-2 mb-4"):
            ui.label("Job:").classes("text-sm text-gray-600")
            render_job_id_badge(job_id)

        # User search using shared component
        async def on_user_select(user_data: dict):
            nonlocal selected_user
            selected_user = user_data
            search_input.value = f"{user_data['name']} ({user_data['email']})"
            results_container.clear()

        search_input, results_container = await render_user_search(
            on_select=on_user_select,
            placeholder="Search users by email or name",
        )

        # Action handlers
        async def do_assign():
            if not selected_user:
                ui.notify("Please select a user", color="negative")
                return

            try:
                async with get_admin_session() as session:
                    stmt = select(Job).where(Job.id == UUID(job_id))
                    result = await session.execute(stmt)
                    job = result.scalar_one_or_none()

                    if not job:
                        ui.notify("Job not found", color="negative")
                        return

                    job.owner_id = selected_user["id"]
                    await session.commit()

                ui.notify("Job assigned successfully", color="positive")
                dialog.close()
                ui.navigate.reload()

            except Exception as e:
                logger.error("Error assigning job: %s", e, exc_info=True)
                ui.notify("Failed to assign job. Check server logs.", color="negative")

        render_dialog_actions(
            on_confirm=do_assign,
            on_cancel=dialog.close,
            confirm_label="Assign",
        )

    dialog.open()


async def render_users_panel():
    """Render the users management panel."""

    with ui.column().classes("w-full gap-4"):
        ui.markdown("## All Users")
        ui.markdown("View all registered users in the system.")

        users_container = ui.column().classes("w-full mt-4")

        async def load_users():
            """Load all users."""
            users_container.clear()

            try:
                async with get_admin_session() as session:
                    stmt = select(User).order_by(User.created_at.desc())
                    result = await session.execute(stmt)
                    users = result.scalars().all()

                    if not users:
                        with users_container:
                            render_empty_state("No users found.")
                        return

                    # Create table
                    columns = [
                        {
                            "name": "name",
                            "label": "Name",
                            "field": "name",
                            "align": "left",
                        },
                        {
                            "name": "email",
                            "label": "Email",
                            "field": "email",
                            "align": "left",
                        },
                        {
                            "name": "created_at",
                            "label": "Joined",
                            "field": "created_at",
                            "align": "left",
                        },
                        {
                            "name": "job_count",
                            "label": "Jobs",
                            "field": "job_count",
                            "align": "center",
                        },
                    ]

                    rows = []
                    for user in users:
                        # Count user's jobs
                        stmt = select(Job).where(Job.owner_id == user.id)
                        result = await session.execute(stmt)
                        job_count = len(result.scalars().all())

                        rows.append(
                            {
                                "id": str(user.id),
                                "name": user.name or "N/A",
                                "email": user.email,
                                "created_at": (
                                    user.created_at.strftime("%Y-%m-%d")
                                    if user.created_at
                                    else "N/A"
                                ),
                                "job_count": job_count,
                            }
                        )

                    with users_container:
                        ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")

            except Exception as e:
                logger.error("Error loading users: %s", e, exc_info=True)
                with users_container:
                    ui.label("Error loading users. Check server logs for details.").classes(
                        "text-red-500"
                    )

        await load_users()


async def render_legacy_user_panel():
    """Render the legacy user creation panel."""

    with ui.column().classes("w-full gap-4"):
        ui.markdown("## Create Legacy User")
        ui.markdown(
            "Create a placeholder 'legacy' user to own orphaned jobs that cannot be "
            "attributed to a specific individual."
        )

        with ui.card().classes("w-full max-w-md mt-4"):
            ui.label("Legacy User Details").classes("text-lg font-bold mb-4")

            name_input = ui.input(
                label="Name",
                placeholder="Legacy User",
                value="Legacy User",
            ).classes("w-full")

            email_input = ui.input(
                label="Email",
                placeholder="legacy@example.com",
                value="legacy@example.com",
            ).classes("w-full")

            async def create_legacy_user():
                """Create the legacy user."""
                if not name_input.value or not email_input.value:
                    ui.notify("Please fill in all fields", color="negative")
                    return

                try:
                    async with get_admin_session() as session:
                        # Check if user already exists
                        stmt = select(User).where(User.email == email_input.value)
                        result = await session.execute(stmt)
                        existing_user = result.scalar_one_or_none()

                        if existing_user:
                            ui.notify(
                                "User with this email already exists",
                                color="warning",
                            )
                            return

                        # Create legacy user
                        user = User(
                            name=name_input.value,
                            email=email_input.value,
                        )
                        session.add(user)
                        await session.commit()

                    ui.notify("Legacy user created successfully", color="positive")

                except Exception as e:
                    logger.error("Error creating legacy user: %s", e, exc_info=True)
                    ui.notify("Failed to create legacy user. Check server logs.", color="negative")

            ui.button(
                "Create Legacy User",
                icon="person_add",
                on_click=create_legacy_user,
            ).props("color=primary").classes("mt-4")

        # Job claim by ID section
        ui.markdown("## Claim Job by ID")
        ui.markdown(
            "Allow users to claim orphaned jobs by entering the job ID. "
            "The job will be assigned to the currently logged-in user."
        )

        with ui.card().classes("w-full max-w-md mt-4"):
            ui.label("Claim Job").classes("text-lg font-bold mb-4")

            job_id_input = ui.input(
                label="Job ID",
                placeholder="job_12345678...",
            ).classes("w-full")

            async def claim_job():
                """Claim a job by ID."""
                job_id = job_id_input.value
                if not job_id:
                    ui.notify("Please enter a job ID", color="negative")
                    return

                # Remove "job_" prefix if present
                if job_id.startswith("job_"):
                    job_id = job_id[4:]

                try:
                    # Get current user ID
                    current_user_id = app.storage.user.get("user_id")
                    if not current_user_id:
                        ui.notify("You must be logged in to claim a job", color="negative")
                        return

                    async with get_admin_session() as session:
                        # Find the job
                        stmt = select(Job).where(Job.id == UUID(job_id))
                        result = await session.execute(stmt)
                        job = result.scalar_one_or_none()

                        if not job:
                            ui.notify("Job not found", color="negative")
                            return

                        if job.owner_id is not None:
                            ui.notify("Job already has an owner", color="warning")
                            return

                        # Claim the job
                        job.owner_id = UUID(current_user_id)
                        await session.commit()

                    ui.notify("Job claimed successfully!", color="positive")
                    job_id_input.value = ""

                except ValueError:
                    ui.notify("Invalid job ID format", color="negative")
                except Exception as e:
                    logger.error("Error claiming job: %s", e, exc_info=True)
                    ui.notify("Failed to claim job. Check server logs.", color="negative")

            ui.button(
                "Claim Job",
                icon="add_circle",
                on_click=claim_job,
            ).props("color=primary").classes("mt-4")


async def render_containers_panel():
    """Render the real-time container dashboard panel."""
    from shandy.webapp_components.utils.container_dashboard import collect_dashboard_data

    _active_timers = setup_timer_cleanup()

    @ui.refreshable
    async def render_dashboard():
        data = await collect_dashboard_data()

        # Container isolation disabled
        if not data.container_isolation_enabled:
            render_alert_banner(
                title="Container Isolation Disabled",
                message=(
                    "Container isolation is not enabled. Set "
                    "SHANDY_USE_CONTAINER_ISOLATION=true to use per-job containers."
                ),
                severity="info",
            )
            return

        # Docker unavailable
        if not data.docker_available:
            render_alert_banner(
                title="Docker Unavailable",
                message=data.error_message or "Docker daemon is not reachable.",
                severity="warning",
            )
            return

        # General error
        if data.error_message:
            render_alert_banner(
                title="Dashboard Error",
                message=data.error_message,
                severity="error",
            )
            return

        # Summary stats row
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

        # No containers running
        if not data.job_groups and not data.orphan_containers:
            render_empty_state("No SHANDY containers are currently running.")
            return

        # Job container groups
        for group in data.job_groups:
            with ui.card().classes("w-full mb-3"):
                # Header row
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

                # Agent container
                if group.agent_container:
                    _render_container_row(group.agent_container, icon="smart_toy")

                # Executor containers
                for ec in group.executor_containers:
                    _render_container_row(ec, icon="code")

                if not group.agent_container and not group.executor_containers:
                    ui.label("No containers").classes("text-gray-400 text-sm px-2 py-1")

        # Orphan containers — persist expansion state across timer refreshes
        if data.orphan_containers:
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

    await render_dashboard()

    @guard_client
    async def guarded_refresh():
        await render_dashboard.refresh()

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
