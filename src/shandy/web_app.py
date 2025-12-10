"""
NiceGUI web interface for SHANDY.

Provides web UI for job submission, monitoring, and results viewing.
"""

import bcrypt
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from nicegui import app, ui
from dotenv import load_dotenv

from .job_manager import JobManager, JobStatus
from .providers import get_provider

# Load environment variables from .env file
# Try Docker path first, fall back to local path
if not load_dotenv("/app/.env", override=True):
    load_dotenv(".env", override=True)

logger = logging.getLogger(__name__)

# Authentication settings
DISABLE_AUTH = os.getenv("DISABLE_AUTH", "false").lower() == "true"
PASSWORD_HASH = os.getenv("APP_PASSWORD_HASH", "").encode()
STORAGE_SECRET = os.getenv("STORAGE_SECRET", "change-this-to-a-random-secret-string-in-production")

if DISABLE_AUTH:
    logger.warning("Authentication is DISABLED! Anyone can access this app.")
    logger.warning("Set DISABLE_AUTH=false in .env to re-enable authentication.")

# Global job manager
job_manager: Optional[JobManager] = None


def init_app(jobs_dir: Path = Path("jobs"), max_concurrent: int = 1):
    """Initialize the web application."""
    global job_manager
    job_manager = JobManager(jobs_dir=jobs_dir, max_concurrent=max_concurrent)

    # Add static file serving for job plots
    app.add_static_files('/jobs', str(jobs_dir))

    logger.info("Web app initialized")


def check_password(password: str) -> bool:
    """Check if password matches the hash"""
    if not PASSWORD_HASH:
        return True  # No password set, allow access
    try:
        return bcrypt.checkpw(password.encode(), PASSWORD_HASH)
    except Exception as e:
        logger.error(f"Password check failed: {e}")
        return False


# Global dict to store uploaded files per session
_uploaded_files = {}


@ui.page("/login")
def login_page():
    """Login page"""
    def try_login():
        if check_password(password_input.value):
            app.storage.user["authenticated"] = True
            ui.navigate.to("/")
        else:
            ui.notify("Invalid password", color="negative")
            password_input.value = ""

    with ui.column().classes("absolute-center items-center"):
        ui.markdown("# SHANDY")
        ui.markdown("_Scientific Hypothesis Agent for Novel Discovery_")
        password_input = ui.input("Password", password=True, password_toggle_button=True).classes("w-64").on("keydown.enter", try_login)
        ui.button("Login", on_click=try_login).classes("w-64")


@ui.page("/")
def index_page():
    """Homepage - redirects to jobs list."""

    # Check authentication (skip if disabled)
    if not DISABLE_AUTH and not app.storage.user.get("authenticated", False):
        ui.navigate.to("/login")
        return

    # Redirect to jobs page
    ui.navigate.to("/jobs")


@ui.page("/new")
def new_job_page():
    """Job submission form."""

    # Check authentication (skip if disabled)
    if not DISABLE_AUTH and not app.storage.user.get("authenticated", False):
        ui.navigate.to("/login")
        return

    # Use client ID as key for this session's uploads
    session_id = str(id(index_page))  # Simple session identifier
    if session_id not in _uploaded_files:
        _uploaded_files[session_id] = []

    def submit_job():
        """Handle job submission."""
        # Validate inputs
        if not research_question.value.strip():
            ui.notify("Please enter a research question", type="negative")
            return

        # Files are optional - no validation needed

        # Generate job ID
        import uuid
        job_id = f"job_{uuid.uuid4().hex[:8]}"

        # Save uploaded files to temp location (if any)
        data_files = []
        if _uploaded_files.get(session_id):
            for uploaded_file in _uploaded_files[session_id]:
                # Create temp file
                temp_file = Path(tempfile.mkdtemp()) / uploaded_file['name']
                with open(temp_file, "wb") as f:
                    f.write(uploaded_file['content'])
                data_files.append(temp_file)

        # Create job
        try:
            job_info = job_manager.create_job(
                job_id=job_id,
                research_question=research_question.value,
                data_files=data_files,
                max_iterations=int(max_iterations.value),
                use_skills=use_skills.value,
                auto_start=True
            )

            ui.notify(f"Job {job_id} created and started!", type="positive")

            # Clear form
            research_question.value = ""
            _uploaded_files[session_id] = []
            upload.reset()

            # Redirect to job detail page
            ui.navigate.to(f"/job/{job_id}")

        except Exception as e:
            ui.notify(f"Error creating job: {e}", type="negative")
            logger.error(f"Error creating job: {e}", exc_info=True)

    async def handle_upload(e):
        """Handle file upload."""
        try:
            # e.file is the uploaded file object
            # e.file.read() is async and returns bytes
            content = await e.file.read()
            name = e.file.name

            _uploaded_files[session_id].append({
                'name': name,
                'content': content
            })
            ui.notify(f"Uploaded: {name}", type="positive")
            logger.info(f"Successfully uploaded {name} ({len(content)} bytes)")
        except Exception as ex:
            logger.error(f"Upload failed: {ex}", exc_info=True)
            ui.notify(f"Upload failed: {str(ex)}", type="negative")

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY").classes("text-h4")
        ui.label("Scientific Hypothesis Agent for Novel Discovery").classes("text-subtitle1")

    # Main content
    with ui.card().classes("w-full max-w-2xl mx-auto mt-8"):
        ui.label("Submit Discovery Job").classes("text-h5 mb-4")

        # Research question
        research_question = ui.textarea(
            label="Research Question",
            placeholder="e.g., What metabolic pathways are affected by hypothermia?",
            validation={"Too short": lambda value: len(value) >= 10}
        ).classes("w-full")

        # File upload
        # Supported: Tabular (CSV, TSV, Excel, Parquet, JSON), Structures (PDB, mmCIF), Sequences (FASTA), Images (PNG, JPG)
        upload = ui.upload(
            label="Upload Data Files (Optional - Tabular, Structures, Sequences, Images)",
            multiple=True,
            auto_upload=True,
            on_upload=handle_upload
        ).classes("w-full")

        # Configuration
        with ui.row().classes("w-full"):
            max_iterations = ui.number(
                label="Max Iterations",
                value=10,
                min=2,
                max=100,
                step=1
            ).classes("flex-1")

            use_skills = ui.checkbox("Use Skills", value=True)

        # Budget info (project-level)
        try:
            provider = get_provider()
            cost_info = provider.get_cost_info(lookback_hours=24)
            budget_check = provider.check_budget_limits()

            with ui.card().classes("w-full bg-blue-50"):
                ui.label("Budget Information").classes("text-subtitle2 font-bold")

                # Display costs or "N/A" if unavailable
                total_spend_str = f"${cost_info.total_spend_usd:.2f}" if cost_info.total_spend_usd is not None else "N/A"
                recent_spend_str = f"${cost_info.recent_spend_usd:.2f}" if cost_info.recent_spend_usd is not None else "N/A"

                ui.label(f"Total Spend: {total_spend_str}").classes("text-sm")
                ui.label(f"Last 24h: {recent_spend_str}").classes("text-sm")

                if cost_info.budget_remaining_usd is not None:
                    ui.label(f"Remaining: ${cost_info.budget_remaining_usd:.2f}").classes("text-sm")

                # Show warnings/errors
                if not budget_check["can_proceed"]:
                    ui.label("⚠️ Budget limit exceeded").classes("text-sm text-negative font-bold")
        except Exception as e:
            with ui.card().classes("w-full bg-yellow-50"):
                ui.label("Budget Information Unavailable").classes("text-subtitle2 font-bold")
                ui.label("Check provider configuration in .env").classes("text-sm text-gray-600")

        # Submit button
        ui.button("Start Discovery", on_click=submit_job).classes("w-full mt-4")

    # Quick links
    with ui.row().classes("w-full max-w-2xl mx-auto mt-4"):
        ui.button("View Jobs", on_click=lambda: ui.navigate.to("/jobs"), icon="list").classes("flex-1")
        ui.button("Documentation", on_click=lambda: ui.navigate.to("/docs"), icon="help").classes("flex-1")


@ui.page("/jobs")
def jobs_page():
    """Jobs list page."""

    # Check authentication (skip if disabled)
    if not DISABLE_AUTH and not app.storage.user.get("authenticated", False):
        ui.navigate.to("/login")
        return

    def refresh_jobs():
        """Refresh jobs table."""
        jobs = job_manager.list_jobs(limit=50)

        # Update table
        table.rows = [
            {
                "job_id": job.job_id,
                "question": job.research_question[:50] + "..." if len(job.research_question) > 50 else job.research_question,
                "status": job.status.value,
                "iterations": f"{job.iterations_completed}/{job.max_iterations}",
                "findings": job.findings_count,
                "created": job.created_at[:19]  # Remove milliseconds
            }
            for job in jobs
        ]
        table.update()

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY - Jobs").classes("text-h4")
        with ui.row():
            ui.button("New Job", on_click=lambda: ui.navigate.to("/new"), icon="add")
            ui.button("Refresh", on_click=refresh_jobs, icon="refresh")

    # Summary cards
    summary = job_manager.get_job_summary()
    with ui.row().classes("w-full gap-4 p-4"):
        with ui.card():
            ui.label("Total Jobs").classes("text-subtitle2")
            ui.label(str(summary["total_jobs"])).classes("text-h4")

        with ui.card():
            ui.label("Running").classes("text-subtitle2")
            ui.label(str(summary["status_counts"].get("running", 0))).classes("text-h4 text-blue-600")

        with ui.card():
            ui.label("Completed").classes("text-subtitle2")
            ui.label(str(summary["status_counts"].get("completed", 0))).classes("text-h4 text-green-600")

    # Cost dashboard (project-level)
    if summary.get("cost_info"):
        cost_info = summary["cost_info"]
        budget_check = summary.get("budget_check", {})

        with ui.card().classes("w-full p-4"):
            ui.label("Project Costs").classes("text-h6 mb-2")

            with ui.row().classes("w-full gap-4"):
                # Total spend
                with ui.column().classes("items-start"):
                    total_spend_display = f"${cost_info.total_spend_usd:.2f}" if cost_info.total_spend_usd is not None else "N/A"
                    ui.label(total_spend_display).classes("text-h4 text-primary")
                    ui.label("Total Spend").classes("text-caption text-grey")

                # Last 24h
                with ui.column().classes("items-start"):
                    recent_spend_display = f"${cost_info.recent_spend_usd:.2f}" if cost_info.recent_spend_usd is not None else "N/A"
                    ui.label(recent_spend_display).classes("text-h5")
                    ui.label(f"Last {cost_info.recent_period_hours}h").classes("text-caption text-grey")

                # Budget remaining (if available)
                if cost_info.budget_remaining_usd is not None:
                    with ui.column().classes("items-start"):
                        remaining_color = "text-positive" if cost_info.budget_remaining_usd > 10 else "text-warning"
                        ui.label(f"${cost_info.budget_remaining_usd:.2f}").classes(f"text-h5 {remaining_color}")
                        ui.label("Budget Remaining").classes("text-caption text-grey")

                # Provider info
                with ui.column().classes("items-start ml-auto"):
                    ui.label(f"Provider: {cost_info.provider_name}").classes("text-caption")
                    if cost_info.data_lag_note:
                        ui.label(cost_info.data_lag_note).classes("text-caption text-grey-6")

            # Budget warnings/errors
            if budget_check and budget_check.get("errors"):
                for error in budget_check["errors"]:
                    ui.label(f"⚠️ {error}").classes("text-caption text-negative font-bold")
            elif budget_check and budget_check.get("warnings"):
                for warning in budget_check["warnings"]:
                    ui.label(f"⚠️ {warning}").classes("text-caption text-warning")
    else:
        with ui.card().classes("w-full bg-yellow-50 p-4"):
            ui.label("Cost tracking unavailable").classes("text-caption text-warning")

    # Jobs table
    table = ui.table(
        columns=[
            {"name": "job_id", "label": "Job ID", "field": "job_id", "align": "left"},
            {"name": "question", "label": "Research Question", "field": "question", "align": "left"},
            {"name": "status", "label": "Status", "field": "status", "align": "center"},
            {"name": "iterations", "label": "Iterations", "field": "iterations", "align": "center"},
            {"name": "findings", "label": "Findings", "field": "findings", "align": "center"},
            {"name": "created", "label": "Created", "field": "created", "align": "left"},
            {"name": "actions", "label": "Actions", "field": "actions", "align": "center"}
        ],
        rows=[],
        row_key="job_id",
        pagination=10
    ).classes("w-full")

    # Add action buttons using slot template
    table.add_slot('body-cell-actions', r'''
        <q-td :props="props">
            <q-btn flat dense color="primary" label="View"
                   @click="$parent.$emit('view-job', props.row.job_id)" />
        </q-td>
    ''')

    table.on('view-job', lambda e: ui.navigate.to(f"/job/{e.args}"))

    # Initial load
    refresh_jobs()


@ui.page("/job/{job_id}")
def job_detail_page(job_id: str):
    """Job detail page with simplified 3-tab UI: Summary, Timeline, Report."""
    import json
    from collections import defaultdict

    # Check authentication (skip if disabled)
    if not DISABLE_AUTH and not app.storage.user.get("authenticated", False):
        ui.navigate.to("/login")
        return

    job_info = job_manager.get_job(job_id)

    if job_info is None:
        ui.label(f"Job {job_id} not found").classes("text-h5")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"))
        return

    job_dir = job_manager.jobs_dir / job_id
    kg_path = job_dir / "knowledge_graph.json"

    # Load knowledge graph data
    kg_data = None
    if kg_path.exists():
        with open(kg_path) as f:
            kg_data = json.load(f)

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label(f"SHANDY - {job_id}").classes("text-h4")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"))

    # Job info cards (compact header)
    with ui.row().classes("w-full gap-4 p-4"):
        with ui.card().classes("flex-1"):
            ui.label("Status").classes("text-subtitle2")
            status_colors = {
                JobStatus.CREATED: "gray",
                JobStatus.QUEUED: "blue",
                JobStatus.RUNNING: "yellow",
                JobStatus.COMPLETED: "green",
                JobStatus.FAILED: "red",
                JobStatus.CANCELLED: "gray"
            }
            color = status_colors.get(job_info.status, "gray")
            ui.badge(job_info.status.value, color=color).classes("text-h6")

        with ui.card().classes("flex-1"):
            ui.label("Progress").classes("text-subtitle2")
            ui.label(f"{job_info.iterations_completed} / {job_info.max_iterations}").classes("text-h5")
            ui.linear_progress(job_info.iterations_completed / job_info.max_iterations if job_info.max_iterations > 0 else 0)

        with ui.card().classes("flex-1"):
            ui.label("Findings").classes("text-subtitle2")
            ui.label(str(job_info.findings_count)).classes("text-h5")

        with ui.card().classes("flex-1"):
            ui.label("Papers Reviewed").classes("text-subtitle2")
            lit_count = len(kg_data.get("literature", [])) if kg_data else 0
            ui.label(str(lit_count)).classes("text-h5")

    # Research question
    with ui.card().classes("w-full"):
        ui.label("Research Question").classes("text-subtitle2 font-bold")
        ui.label(job_info.research_question)

    # Error message if failed
    if job_info.status == JobStatus.FAILED and job_info.error:
        with ui.card().classes("w-full bg-red-50"):
            ui.label("Error").classes("text-subtitle2 font-bold text-red-800")
            ui.label(job_info.error).classes("text-red-600")

    # NEW: 3-tab structure
    with ui.tabs().classes("w-full") as tabs:
        summary_tab = ui.tab("Summary")
        timeline_tab = ui.tab("Timeline")
        report_tab = ui.tab("Report")

    # Store reference to tabs for cross-linking
    tabs_ref = tabs
    timeline_tab_ref = timeline_tab

    with ui.tab_panels(tabs, value=summary_tab).classes("w-full"):
        # ===== SUMMARY TAB =====
        with ui.tab_panel(summary_tab):
            if kg_data:
                findings = kg_data.get("findings", [])

                if findings:
                    ui.label("Key Findings").classes("text-h6 mb-2")

                    for i, finding in enumerate(findings, 1):
                        iteration_discovered = finding.get("iteration_discovered", "?")
                        with ui.card().classes("w-full mb-2 cursor-pointer hover:bg-gray-50") as finding_card:
                            with ui.row().classes("w-full items-start"):
                                with ui.column().classes("flex-1"):
                                    ui.label(f"{i}. {finding['title']}").classes("text-subtitle1 font-bold")
                                    ui.label(finding["evidence"]).classes("text-sm mt-1")
                                    interpretation = finding.get("biological_interpretation") or finding.get("interpretation", "")
                                    if interpretation:
                                        ui.label(interpretation).classes("text-sm text-gray-600 mt-1")

                                # Link to iteration
                                with ui.column().classes("items-end"):
                                    iter_badge = ui.badge(f"Iter {iteration_discovered}", color="blue").classes("cursor-pointer")
                                    iter_badge.on("click", lambda e, it=iteration_discovered: jump_to_iteration(it))
                else:
                    ui.label("No findings yet. Check the Timeline tab to see the agent's progress.").classes("text-gray-500")

                # Quick stats
                ui.label("Investigation Summary").classes("text-h6 mt-4 mb-2")
                with ui.row().classes("gap-4"):
                    with ui.card().classes("p-3"):
                        ui.label(f"{kg_data.get('iteration', 0)}").classes("text-h5")
                        ui.label("Iterations").classes("text-caption")
                    with ui.card().classes("p-3"):
                        ui.label(f"{len(kg_data.get('findings', []))}").classes("text-h5")
                        ui.label("Findings").classes("text-caption")
                    with ui.card().classes("p-3"):
                        ui.label(f"{len(kg_data.get('literature', []))}").classes("text-h5")
                        ui.label("Papers").classes("text-caption")
                    with ui.card().classes("p-3"):
                        ui.label(f"{len(kg_data.get('analysis_log', []))}").classes("text-h5")
                        ui.label("Analyses").classes("text-caption")
            else:
                ui.label("No data available yet").classes("text-gray-500")

        # ===== TIMELINE TAB =====
        with ui.tab_panel(timeline_tab):
            timeline_container = ui.column().classes("w-full")

            def build_timeline():
                timeline_container.clear()
                with timeline_container:
                    if not kg_data:
                        ui.label("No timeline data available").classes("text-gray-500")
                        return

                    # Group analysis log by iteration
                    by_iteration = defaultdict(list)
                    for entry in kg_data.get("analysis_log", []):
                        by_iteration[entry["iteration"]].append(entry)

                    # Get iteration summaries (agent-generated)
                    iteration_summaries = {
                        s["iteration"]: s["summary"]
                        for s in kg_data.get("iteration_summaries", [])
                    }

                    # Get findings by iteration for highlighting
                    findings_by_iteration = defaultdict(list)
                    for finding in kg_data.get("findings", []):
                        findings_by_iteration[finding.get("iteration_discovered", 0)].append(finding)

                    if not by_iteration:
                        ui.label("No iterations recorded yet").classes("text-gray-500")
                        return

                    ui.label("Investigation Timeline").classes("text-h6 mb-2")

                    with ui.scroll_area().classes("w-full"):
                        for iteration in sorted(by_iteration.keys()):
                            entries = by_iteration[iteration]

                            # Get agent-generated summary or fall back to synthesized
                            agent_summary = iteration_summaries.get(iteration)
                            if agent_summary:
                                summary_text = agent_summary
                            else:
                                # Fallback: synthesize from log entries
                                summary_text = _synthesize_iteration_summary(entries)

                            # Check if this iteration had findings
                            has_findings = len(findings_by_iteration.get(iteration, [])) > 0
                            card_class = "w-full mb-2 border-l-4 border-green-500" if has_findings else "w-full mb-2"

                            with ui.expansion(
                                f"Iteration {iteration}: {summary_text}",
                                icon="check_circle" if has_findings else "science"
                            ).classes(card_class).props(f'id="iteration-{iteration}"') as expansion:

                                # Show findings from this iteration
                                iter_findings = findings_by_iteration.get(iteration, [])
                                if iter_findings:
                                    ui.label("Findings Recorded:").classes("font-bold mt-2 mb-1")
                                    for finding in iter_findings:
                                        with ui.card().classes("w-full bg-green-50 mb-2"):
                                            ui.label(finding["title"]).classes("font-bold")
                                            ui.label(finding["evidence"]).classes("text-sm")

                                # Show plots from this iteration
                                plots_dir = job_dir / "plots"
                                iteration_plots = []
                                if plots_dir.exists():
                                    for plot_file in plots_dir.glob("*.png"):
                                        metadata_file = plot_file.with_suffix('.json')
                                        if metadata_file.exists():
                                            with open(metadata_file) as mf:
                                                metadata = json.load(mf)
                                            if metadata.get("iteration") == iteration:
                                                iteration_plots.append((plot_file, metadata))

                                if iteration_plots:
                                    ui.label(f"Plots ({len(iteration_plots)}):").classes("font-bold mt-2 mb-1")
                                    with ui.grid(columns=2).classes("w-full gap-2"):
                                        for plot_file, metadata in iteration_plots:
                                            plot_title = plot_file.stem.replace('_', ' ').title()
                                            description = metadata.get('description', plot_title)
                                            with ui.card().classes("p-2"):
                                                ui.label(plot_title).classes("text-sm font-bold")
                                                if description:
                                                    ui.label(description).classes("text-xs text-blue-700 italic")
                                                plot_url = f"/{plot_file}"
                                                ui.image(plot_url).classes("w-full")

                                # Show literature searched in this iteration
                                lit_this_iter = [
                                    lit for lit in kg_data.get("literature", [])
                                    if lit.get("retrieved_at_iteration") == iteration
                                ]
                                if lit_this_iter:
                                    ui.label(f"Literature Searched ({len(lit_this_iter)}):").classes("font-bold mt-2 mb-1")
                                    for lit in lit_this_iter[:3]:  # Show first 3
                                        ui.label(f"- {lit['title'][:80]}...").classes("text-sm text-gray-600")
                                    if len(lit_this_iter) > 3:
                                        ui.label(f"  + {len(lit_this_iter) - 3} more papers").classes("text-sm text-gray-500")

                                # Show code executions count
                                code_execs = [e for e in entries if e["action"] == "execute_code"]
                                if code_execs:
                                    ui.label(f"Analyses Run: {len(code_execs)}").classes("text-sm text-gray-600 mt-2")

            build_timeline()

        # ===== REPORT TAB =====
        with ui.tab_panel(report_tab):
            report_path = job_dir / "final_report.md"
            pdf_path = job_dir / "final_report.pdf"

            if report_path.exists():
                # Download buttons at top
                with ui.row().classes("w-full justify-end mb-4 gap-2"):
                    ui.button(
                        "Download Markdown",
                        on_click=lambda: ui.download(report_path.read_bytes(), filename=f"{job_id}_report.md"),
                        icon="download"
                    ).props("color=secondary outline")

                    if pdf_path.exists():
                        ui.button(
                            "Download PDF",
                            on_click=lambda: ui.download(pdf_path.read_bytes(), filename=f"{job_id}_report.pdf"),
                            icon="picture_as_pdf"
                        ).props("color=primary")
                    else:
                        ui.button(
                            "PDF Unavailable",
                            icon="picture_as_pdf"
                        ).props("color=grey outline disabled")

                # Display markdown
                with open(report_path) as f:
                    report_content = f.read()
                ui.markdown(report_content).classes("w-full")
            else:
                if job_info.status == JobStatus.COMPLETED:
                    ui.label("Report generation failed").classes("text-red-500")
                else:
                    ui.label("Report not yet available (job not complete)").classes("text-gray-500")

    # Helper function to jump to iteration in timeline
    def jump_to_iteration(iteration: int):
        """Switch to Timeline tab and scroll to specific iteration."""
        tabs_ref.set_value(timeline_tab_ref)
        # Use JavaScript to scroll to the iteration element
        ui.run_javascript(f'''
            setTimeout(() => {{
                const el = document.querySelector('[id="iteration-{iteration}"]');
                if (el) el.scrollIntoView({{ behavior: "smooth", block: "center" }});
            }}, 100);
        ''')

    # Action buttons
    with ui.row().classes("mt-4"):
        if job_info.status in [JobStatus.RUNNING, JobStatus.QUEUED]:
            ui.button(
                "Cancel Job",
                on_click=lambda: cancel_job(job_id),
                color="red"
            )

        if job_info.status in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]:
            ui.button(
                "Delete Job",
                on_click=lambda: delete_job(job_id),
                color="red"
            )

    def cancel_job(jid):
        """Cancel the job."""
        try:
            job_manager.cancel_job(jid)
            ui.notify(f"Job {jid} cancelled", type="positive")
            ui.navigate.to("/jobs")
        except Exception as e:
            ui.notify(f"Error cancelling job: {e}", type="negative")

    def delete_job(jid):
        """Delete the job."""
        try:
            job_manager.delete_job(jid)
            ui.notify(f"Job {jid} deleted", type="positive")
            ui.navigate.to("/jobs")
        except Exception as e:
            ui.notify(f"Error deleting job: {e}", type="negative")


def _synthesize_iteration_summary(entries: list) -> str:
    """
    Synthesize a summary from analysis log entries when no agent summary is available.

    This is the fallback for older jobs that don't have agent-generated summaries.
    """
    code_executions = [e for e in entries if e["action"] == "execute_code"]
    literature_searches = [e for e in entries if e["action"] == "search_pubmed"]
    findings_recorded = [e for e in entries if e["action"] == "update_knowledge_graph"]

    summary_parts = []

    # Describe analyses
    if code_executions:
        for ce in code_executions[:2]:
            desc = ce.get("description", "")
            if desc:
                summary_parts.append(desc)
            else:
                code = ce.get("code", "")
                if "correlation" in code.lower():
                    summary_parts.append("correlation analysis")
                elif "t.test" in code or "ttest" in code.lower():
                    summary_parts.append("t-test")
                elif "pca" in code.lower():
                    summary_parts.append("PCA analysis")
                elif "plot" in code.lower() or "plt." in code:
                    summary_parts.append("visualization")
                else:
                    summary_parts.append("data analysis")
        if len(code_executions) > 2:
            summary_parts.append(f"+ {len(code_executions) - 2} more")

    # Describe literature searches
    if literature_searches:
        query = literature_searches[0].get("query", "")
        if query:
            summary_parts.append(f'searched "{query[:40]}..."')

    # Mention findings
    if findings_recorded:
        summary_parts.append(f"recorded {len(findings_recorded)} finding{'s' if len(findings_recorded) > 1 else ''}")

    return ", ".join(summary_parts) if summary_parts else "Processing..."


@ui.page("/docs")
def docs_page():
    """Documentation page."""

    # Check authentication (skip if disabled)
    if not DISABLE_AUTH and not app.storage.user.get("authenticated", False):
        ui.navigate.to("/login")
        return

    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY - Documentation").classes("text-h4")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"), icon="arrow_back")

    with ui.card().classes("w-full max-w-4xl mx-auto mt-8"):
        ui.markdown("""
# SHANDY Documentation

**Scientific Hypothesis Agent for Novel Discovery**

## What is SHANDY?

SHANDY is an autonomous AI scientist that analyzes scientific data to discover mechanistic insights through iterative hypothesis testing.

## How It Works

1. **Submit a Job**: Upload your data files (CSV) and provide a research question
2. **Autonomous Discovery**: SHANDY runs for N iterations, testing hypotheses and searching literature
3. **Results**: Get findings, mechanistic models, and a final report

## Features

- **Autonomous**: Runs without human intervention
- **Domain-Agnostic**: Works for metabolomics, genomics, structural biology, and more
- **Skills-Based**: Uses structured workflows for scientific rigor
- **Cost-Tracked**: Budget monitoring with provider-specific tracking
- **Literature-Grounded**: Searches PubMed for mechanistic insights

## Skills

SHANDY uses two types of skills:

### Workflow Skills
- Hypothesis generation
- Result interpretation
- Prioritization
- Stopping criteria

### Domain Skills
- Metabolomics
- Genomics/Transcriptomics
- Data Science/Statistics

## Tips for Success

1. **Clear Research Question**: Be specific about what you want to discover
2. **Clean Data**: Ensure CSV files are properly formatted
3. **Appropriate Iterations**: 30-50 iterations for most analyses
4. **Enable Skills**: Skills provide structure and prevent common mistakes

## Data Format

CSV files should have:
- First row: Column headers
- First column: Sample IDs
- Second column (optional): Group labels
- Remaining columns: Features (metabolites, genes, etc.)

Example:
```csv
sample_id,group,metabolite1,metabolite2,...
sample1,control,100,200,...
sample2,treatment,150,180,...
```

## Budget

SHANDY tracks project-level costs through your configured model provider (CBORG, Vertex AI, or Bedrock). Check your budget before starting large jobs.

## Support

For issues or questions, contact your system administrator.
        """)


def main(host: str = "0.0.0.0", port: int = 8080, jobs_dir: Path = Path("jobs")):
    """
    Run the web application.

    Args:
        host: Host to bind to
        port: Port to bind to
        jobs_dir: Directory for jobs
    """
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Initialize app
    init_app(jobs_dir=jobs_dir)

    # Run NiceGUI
    ui.run(
        host=host,
        port=port,
        title="SHANDY",
        reload=False,
        show=False,  # Don't auto-open browser in Docker
        storage_secret=STORAGE_SECRET  # Required for app.storage.user
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SHANDY Web Interface")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument("--jobs-dir", default="jobs", help="Jobs directory")

    args = parser.parse_args()

    main(host=args.host, port=args.port, jobs_dir=Path(args.jobs_dir))
