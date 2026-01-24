"""
NiceGUI web interface for SHANDY.

Provides web UI for job submission, monitoring, and results viewing.
"""

import bcrypt
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from nicegui import app, ui
from dotenv import load_dotenv

from .job_manager import JobManager, JobStatus
from .providers import get_provider


def get_action_description(tool_use: Dict[str, Any]) -> str:
    """
    Get description for a tool use action with fallback logic.

    Args:
        tool_use: Tool use object from transcript

    Returns:
        Description string
    """
    inp = tool_use.get("input", {})

    # 1. Explicit description
    if inp.get("description"):
        return inp["description"]

    # 2. Tool-specific fallback from key inputs
    name = tool_use.get("name", "")
    if "search_pubmed" in name:
        return f"Search: {inp.get('query', '')}"
    if "update_knowledge_state" in name:
        return f"Finding: {inp.get('title', '')}"
    if "save_iteration_summary" in name:
        return f"Summary: {inp.get('summary', '')[:50]}..."
    if "execute_code" in name:
        return "Code execution"

    # 3. Just the tool name
    return name.split("__")[-1] if "__" in name else name


def parse_transcript_actions(transcript: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Parse a transcript to extract actions with their reasoning and results.

    Args:
        transcript: List of transcript entries from iterN_transcript.json

    Returns:
        List of action dicts with: tool_name, description, input, result, success
    """
    actions = []
    tool_results = {}

    # First pass: collect all tool results by tool_use_id
    for entry in transcript:
        if entry.get("type") == "user":
            content = entry.get("message", {}).get("content", [])
            for item in content:
                if item.get("type") == "tool_result":
                    tool_use_id = item.get("tool_use_id")
                    result_content = item.get("content", "")
                    # Parse JSON string if it looks like one
                    if isinstance(result_content, str) and result_content.startswith("{"):
                        try:
                            result_content = json.loads(result_content)
                        except json.JSONDecodeError:
                            pass
                    tool_results[tool_use_id] = result_content

    # Second pass: collect tool uses and match with results
    for entry in transcript:
        if entry.get("type") == "assistant":
            content = entry.get("message", {}).get("content", [])
            for item in content:
                if item.get("type") == "tool_use":
                    tool_use_id = item.get("id")
                    tool_name = item.get("name", "")
                    inp = item.get("input", {})

                    # Skip non-shandy tools (like Read, Bash, etc.)
                    if "shandy" not in tool_name.lower():
                        continue

                    # Get the result
                    result = tool_results.get(tool_use_id, {})
                    result_text = ""
                    success = True

                    if isinstance(result, dict):
                        result_text = result.get("result", str(result))
                    elif isinstance(result, str):
                        result_text = result
                    elif isinstance(result, list):
                        # Handle list of content items
                        result_text = str(result)

                    # Determine success from result text
                    if "failed" in result_text.lower() or "error" in result_text.lower():
                        success = False

                    actions.append({
                        "tool_name": tool_name,
                        "short_name": tool_name.split("__")[-1] if "__" in tool_name else tool_name,
                        "description": get_action_description(item),
                        "input": inp,
                        "result": result_text,
                        "success": success,
                    })

    return actions

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
            # Determine investigation mode
            mode = "coinvestigate" if coinvestigate_mode.value else "autonomous"

            job_info = job_manager.create_job(
                job_id=job_id,
                research_question=research_question.value,
                data_files=data_files,
                max_iterations=int(max_iterations.value),
                use_skills=True,
                auto_start=True,
                investigation_mode=mode
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
        max_iterations = ui.number(
            label="Max Iterations",
            value=10,
            min=2,
            max=100,
            step=1
        ).classes("w-full")

        # Advanced options (collapsed by default)
        with ui.expansion("Advanced Options (Experimental)", icon="science").classes("w-full mt-4"):
            with ui.card().classes("w-full"):
                coinvestigate_mode = ui.switch("Coinvestigate Mode", value=False)
                ui.label("Requires your active participation. After each iteration, I will pause to receive your feedback.").classes("text-sm text-gray-700 mt-1")
                ui.label("Requires you to stay near your computer. Auto-continues after 15 min if you don't respond.").classes("text-xs text-orange-700")

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
        jobs = job_manager.list_jobs()

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
            ui.button("Billing", on_click=lambda: ui.navigate.to("/billing"), icon="payments").props("flat")

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
    """Job detail page with progressive disclosure UI."""
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
    ks_path = job_dir / "knowledge_state.json"

    # Track current status for polling
    current_status = {"value": job_info.status}

    # Load knowledge state data (with error handling for concurrent writes)
    ks_data = None
    ks_load_error = None
    if ks_path.exists():
        try:
            with open(ks_path) as f:
                ks_data = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse knowledge_state.json for {job_id}: {e}")
            ks_load_error = "Knowledge state is being updated. Please refresh the page."

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label(f"SHANDY - {job_id}").classes("text-h4")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"))

    # Error message if failed (show prominently at top)
    if job_info.status == JobStatus.FAILED and job_info.error:
        with ui.card().classes("w-full bg-red-50 m-4"):
            ui.label("Error").classes("text-subtitle2 font-bold text-red-800")
            ui.label(job_info.error).classes("text-red-600")

    # Warning if knowledge state couldn't be loaded (e.g., concurrent write)
    if ks_load_error:
        with ui.card().classes("w-full bg-yellow-50 m-4"):
            ui.label("Loading...").classes("text-subtitle2 font-bold text-yellow-800")
            ui.label(ks_load_error).classes("text-yellow-600")

    # 2-Tab Structure: Research Log (primary), Report
    with ui.tabs().classes("w-full") as tabs:
        timeline_tab = ui.tab("Research Log")
        report_tab = ui.tab("Report")

    with ui.tab_panels(tabs, value=timeline_tab).classes("w-full"):
        # ===== TIMELINE TAB (Primary View) =====
        with ui.tab_panel(timeline_tab):
            # Status cards row (moved from Summary)
            status_colors = {
                JobStatus.CREATED: "gray",
                JobStatus.QUEUED: "blue",
                JobStatus.RUNNING: "yellow",
                JobStatus.COMPLETED: "green",
                JobStatus.FAILED: "red",
                JobStatus.CANCELLED: "gray"
            }

            with ui.row().classes("w-full gap-4 mb-4"):
                with ui.card().classes("flex-1"):
                    ui.label("Status").classes("text-subtitle2")
                    color = status_colors.get(job_info.status, "gray")
                    ui.badge(job_info.status.value, color=color).classes("text-h6")

                with ui.card().classes("flex-1"):
                    ui.label("Progress").classes("text-subtitle2")
                    progress = job_info.iterations_completed / max(job_info.max_iterations, 1)
                    ui.label(f"{job_info.iterations_completed} / {job_info.max_iterations}").classes("text-h5")
                    ui.linear_progress(progress)

                with ui.card().classes("flex-1"):
                    ui.label("Findings").classes("text-subtitle2")
                    ui.label(str(job_info.findings_count)).classes("text-h5 text-green-600")

                with ui.card().classes("flex-1"):
                    ui.label("Papers Reviewed").classes("text-subtitle2")
                    lit_count = len(ks_data.get("literature", [])) if ks_data else 0
                    ui.label(str(lit_count)).classes("text-h5 text-blue-600")

            # Research question
            with ui.card().classes("w-full mb-4"):
                ui.label("Research Question").classes("text-subtitle2 font-bold")
                ui.label(job_info.research_question).classes("text-lg")

            # Investigation Timeline
            ui.label("Investigation Timeline").classes("text-h6 font-bold mb-2")

            if ks_data:
                # Get iteration summaries (agent-generated) - includes strapline and full summary
                iteration_summaries = {
                    s["iteration"]: {
                        "summary": s.get("summary", ""),
                        "strapline": s.get("strapline", "")
                    }
                    for s in ks_data.get("iteration_summaries", [])
                }

                # Group analysis log by iteration
                by_iteration = defaultdict(list)
                for entry in ks_data.get("analysis_log", []):
                    by_iteration[entry["iteration"]].append(entry)

                # Get max iteration
                max_iter = ks_data.get("iteration", 1)

                if by_iteration or iteration_summaries:
                    with ui.scroll_area().classes("w-full h-[600px]"):
                        # Display in chronological order (oldest first)
                        # Don't show the current in-progress iteration if awaiting feedback
                        display_max = max_iter - 1 if job_info.status == JobStatus.AWAITING_FEEDBACK else max_iter
                        for iteration in range(1, display_max + 1):
                            entries = by_iteration.get(iteration, [])

                            # Check if this iteration is still in progress
                            # max_iter is the CURRENT iteration being worked on
                            is_in_progress = (iteration == max_iter and job_info.status == JobStatus.RUNNING)

                            # Get agent summary - prefer strapline for header, full summary inside
                            iter_summary = iteration_summaries.get(iteration, {})
                            if isinstance(iter_summary, str):
                                # Backwards compat: old format was just the summary string
                                strapline = ""
                                summary_text = iter_summary
                            else:
                                strapline = iter_summary.get("strapline", "")
                                summary_text = iter_summary.get("summary", "")

                            # Get counts from analysis_log (lightweight, already loaded)
                            # Transcript is loaded lazily when expansion is opened
                            provenance_dir = job_dir / "provenance"
                            code_count = len([e for e in entries if e["action"] == "execute_code"])
                            search_count = len([e for e in entries if e["action"] == "search_pubmed"])
                            finding_count = len([e for e in entries if e["action"] == "update_knowledge_state"])

                            # Determine color based on outcome
                            border_class = "border-l-4 border-gray-300"
                            if finding_count > 0:
                                border_class = "border-l-4 border-green-500"  # Found something!
                            elif code_count > 0 or search_count > 0:
                                border_class = "border-l-4 border-blue-300"  # Did work

                            # Use strapline for header if available, otherwise truncated summary
                            # Add "[in progress]" suffix for iterations still being worked on
                            if strapline:
                                header_text = f"{strapline} [in progress]" if is_in_progress else strapline
                            elif summary_text:
                                truncated = summary_text[:80] + "..." if len(summary_text) > 80 else summary_text
                                header_text = f"{truncated} [in progress]" if is_in_progress else truncated
                            elif is_in_progress:
                                header_text = "Investigation in progress..."
                            else:
                                header_text = "Completed"

                            with ui.expansion(icon="science").classes(f"w-full mb-2 {border_class}") as expansion:
                                # Custom header with badges using slot
                                with expansion.add_slot("header"):
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.label(f"Iteration {iteration}: {header_text}").classes("font-medium")
                                        if code_count:
                                            ui.badge(f"{code_count} analyses", color="blue").props("outline")
                                        if search_count:
                                            ui.badge(f"{search_count} searches", color="purple").props("outline")
                                        if finding_count:
                                            ui.badge(f"{finding_count} findings", color="green")

                                # Container for lazy-loaded content
                                content_container = ui.column().classes("w-full")
                                content_loaded = {"value": False}  # Track if content has been loaded

                                def load_iteration_content(
                                    container,
                                    loaded_flag,
                                    iter_num=iteration,
                                    iter_summary_text=summary_text,
                                    iter_entries=entries,
                                    iter_ks_data=ks_data,
                                    iter_job_dir=job_dir,
                                    iter_provenance_dir=provenance_dir
                                ):
                                    """Lazy load iteration content when expansion is opened."""
                                    if loaded_flag["value"]:
                                        return  # Already loaded
                                    loaded_flag["value"] = True
                                    container.clear()

                                    with container:
                                        # Show full summary if available
                                        if iter_summary_text:
                                            with ui.expansion("Summary", icon="summarize", value=True).classes("w-full mt-2"):
                                                ui.label(iter_summary_text).classes("text-sm text-gray-700")

                                        # Show findings recorded
                                        iteration_findings = [
                                            f for f in iter_ks_data.get("findings", [])
                                            if f.get("iteration_discovered") == iter_num
                                        ]
                                        if iteration_findings:
                                            with ui.expansion(f"Findings ({len(iteration_findings)})", icon="lightbulb").classes("w-full mt-2"):
                                                for finding in iteration_findings:
                                                    with ui.card().classes("w-full mb-2 bg-green-50"):
                                                        ui.label(finding['title']).classes("font-bold text-green-800")
                                                        ui.label(finding["evidence"]).classes("text-sm text-gray-700")
                                                        interpretation = finding.get("biological_interpretation") or finding.get("interpretation", "")
                                                        if interpretation:
                                                            ui.label(interpretation).classes("text-sm text-gray-600 italic mt-1")

                                        # Load transcript lazily (this is the heavy part)
                                        transcript_path = iter_provenance_dir / f"iter{iter_num}_transcript.json"
                                        transcript_actions = []
                                        if transcript_path.exists():
                                            try:
                                                with open(transcript_path) as tf:
                                                    transcript = json.load(tf)
                                                transcript_actions = parse_transcript_actions(transcript)
                                            except Exception as e:
                                                logger.warning(f"Failed to load transcript for iter {iter_num}: {e}")

                                        # Show actions from transcript
                                        if transcript_actions:
                                            with ui.expansion(f"Actions ({len(transcript_actions)})", icon="build").classes("w-full mt-2"):
                                                for action in transcript_actions:
                                                    success = action.get("success", True)
                                                    status_icon = "✅" if success else "❌"
                                                    desc = action.get("description", action.get("short_name", "Unknown"))
                                                    tool_name = action.get("short_name", "")

                                                    if "execute_code" in action.get("tool_name", ""):
                                                        card_class = "w-full mb-2 border-l-4 border-blue-300"
                                                    elif "search_pubmed" in action.get("tool_name", ""):
                                                        card_class = "w-full mb-2 border-l-4 border-purple-300"
                                                    elif "update_knowledge_state" in action.get("tool_name", ""):
                                                        card_class = "w-full mb-2 border-l-4 border-green-300"
                                                    else:
                                                        card_class = "w-full mb-2 border-l-4 border-gray-300"

                                                    with ui.card().classes(card_class):
                                                        with ui.row().classes("items-center gap-2"):
                                                            ui.label(f"{status_icon} {desc}").classes("font-medium text-sm")
                                                            ui.badge(tool_name, color="gray").props("outline").classes("text-xs")

                                                        inp = action.get("input", {})
                                                        if "execute_code" in action.get("tool_name", "") and inp.get("code"):
                                                            with ui.expansion("Code", icon="code").classes("w-full mt-1"):
                                                                ui.code(inp["code"], language="python").classes("text-xs")

                                                        if "search_pubmed" in action.get("tool_name", "") and inp.get("query"):
                                                            ui.label(f"Query: \"{inp['query']}\"").classes("text-xs text-gray-600 mt-1")

                                                        result_text = action.get("result", "")
                                                        if result_text and len(str(result_text)) > 0:
                                                            result_str = str(result_text)
                                                            if len(result_str) > 200:
                                                                with ui.expansion("Result", icon="output").classes("w-full mt-1"):
                                                                    ui.code(result_str[:2000] + ("..." if len(result_str) > 2000 else ""), language="text").classes("text-xs")
                                                            elif not success:
                                                                ui.label(result_str).classes("text-xs text-red-600 mt-1")

                                        # Show plots from this iteration
                                        if iter_provenance_dir.exists():
                                            iteration_plots = []
                                            for plot_file in sorted(iter_provenance_dir.glob("*.png")):
                                                metadata_file = plot_file.with_suffix('.json')
                                                if metadata_file.exists():
                                                    with open(metadata_file) as mf:
                                                        metadata = json.load(mf)
                                                    if metadata.get("iteration") == iter_num:
                                                        iteration_plots.append((plot_file, metadata))

                                            if iteration_plots:
                                                with ui.expansion(f"Visualizations ({len(iteration_plots)})", icon="insert_chart").classes("w-full mt-2"):
                                                    with ui.grid(columns=2).classes("w-full gap-2"):
                                                        for plot_file, metadata in iteration_plots:
                                                            plot_title = plot_file.stem.replace('_', ' ').title()
                                                            description = metadata.get('description', '')

                                                            with ui.card().classes("p-2"):
                                                                ui.label(plot_title).classes("text-sm font-bold")
                                                                if description:
                                                                    ui.label(description).classes("text-xs text-blue-700 italic")
                                                                plot_url = f"/{plot_file}"
                                                                ui.image(plot_url).classes("w-full")

                                                                ui.button(
                                                                    "Download",
                                                                    on_click=lambda p=plot_file: ui.download(p.read_bytes(), filename=p.name),
                                                                    icon="download"
                                                                ).props("size=sm flat dense").classes("mt-2")

                                                                plot_code = metadata.get('code')
                                                                if plot_code:
                                                                    with ui.expansion("View code", icon="code").classes("w-full mt-1"):
                                                                        ui.code(plot_code, language="python").classes("text-xs")

                                        # Show literature searched
                                        literature_entries = [e for e in iter_entries if e["action"] == "search_pubmed"]
                                        if literature_entries:
                                            total_papers = sum(e.get("results_count", 0) for e in literature_entries)
                                            with ui.expansion(f"Literature searched ({total_papers} papers)", icon="article").classes("w-full mt-2"):
                                                for entry in literature_entries:
                                                    query = entry.get("query", "")
                                                    matching_papers = [
                                                        lit for lit in iter_ks_data.get("literature", [])
                                                        if lit.get("search_query") == query
                                                        and lit.get("retrieved_at_iteration") == iter_num
                                                    ]
                                                    if matching_papers:
                                                        with ui.expansion(f'"{query}" ({len(matching_papers)} papers)').classes("w-full"):
                                                            for paper in matching_papers:
                                                                with ui.card().classes("w-full mb-1 p-2"):
                                                                    ui.label(paper.get("title", "Untitled")).classes("text-sm font-bold")
                                                                    pmid = paper.get("pmid", "")
                                                                    if pmid:
                                                                        ui.link(
                                                                            f"PMID: {pmid}",
                                                                            f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                                                                            new_tab=True
                                                                        ).classes("text-xs text-blue-600")
                                                                    abstract = paper.get("abstract", "")
                                                                    if abstract:
                                                                        ui.label(abstract[:200] + "..." if len(abstract) > 200 else abstract).classes("text-xs text-gray-600 mt-1")
                                                    else:
                                                        ui.label(f'"{query}" (0 results)').classes("text-sm text-gray-400 italic")

                                # Show loading placeholder initially
                                with content_container:
                                    ui.label("Click to load details...").classes("text-sm text-gray-400 italic")

                                # Trigger lazy load when expansion is opened
                                # NOTE: Must capture load_iteration_content with default arg, otherwise
                                # all callbacks will use the last iteration's function (closure bug)
                                expansion.on_value_change(
                                    lambda e, cc=content_container, lf=content_loaded, fn=load_iteration_content: fn(cc, lf) if e.value else None
                                )
                else:
                    ui.label("No investigation activity yet").classes("text-gray-500")

                # Dynamic feedback panel container (updates via polling)
                feedback_container = ui.column().classes("w-full")

                def build_feedback_panel():
                    """Build or rebuild the feedback panel based on current status."""
                    feedback_container.clear()

                    # Re-check job status
                    latest_job = job_manager.get_job(job_id)
                    if latest_job is None:
                        return

                    if latest_job.status == JobStatus.AWAITING_FEEDBACK:
                        # Reload KS to get current iteration (which is the NEXT iteration to run)
                        next_iter = 1
                        awaiting_since = None
                        if ks_path.exists():
                            with open(ks_path) as f:
                                latest_ks =json.load(f)
                            next_iter = latest_ks.get("iteration", 1)
                        # The completed iteration is the previous one
                        completed_iter = next_iter - 1 if next_iter > 1 else 1

                        # Get awaiting_feedback_since from config
                        config_path = job_dir / "config.json"
                        if config_path.exists():
                            with open(config_path) as f:
                                cfg = json.load(f)
                            awaiting_since = cfg.get("awaiting_feedback_since")

                        with feedback_container:
                            with ui.card().classes("w-full mt-2 bg-yellow-50 border-2 border-yellow-400"):
                                ui.label(f"Iteration {completed_iter} Complete - Awaiting Your Input").classes("text-h6 font-bold text-yellow-800")
                                ui.label("Provide guidance for the next iteration, or continue without feedback.").classes("text-sm text-gray-700 mb-2")

                                feedback_input = ui.textarea(
                                    label="Your Feedback (optional)",
                                    placeholder="e.g., Focus on metabolic pathways, or investigate the correlation with gene X..."
                                ).classes("w-full")

                                with ui.row().classes("w-full gap-2 mt-2"):
                                    def submit_feedback(fi=feedback_input, ci=completed_iter):
                                        from .knowledge_state import KnowledgeState
                                        ks =KnowledgeState.load(job_dir / "knowledge_state.json")
                                        if fi.value.strip():
                                            ks.add_feedback(fi.value.strip(), ci)
                                            ks.save(job_dir / "knowledge_state.json")
                                        # Set status back to running to signal continue
                                        with open(job_dir / "config.json") as f:
                                            cfg = json.load(f)
                                        cfg["status"] = "running"
                                        with open(job_dir / "config.json", "w") as f:
                                            json.dump(cfg, f, indent=2)
                                        ui.notify("Continuing to next iteration", type="positive")
                                        ui.navigate.to(f"/job/{job_id}")

                                    ui.button("Submit & Continue", on_click=submit_feedback, icon="send").props("color=primary")
                                    ui.button("Continue Without Feedback", on_click=submit_feedback, icon="arrow_forward").props("color=secondary outline")

                                # Countdown timer
                                if awaiting_since:
                                    from datetime import datetime
                                    try:
                                        started = datetime.fromisoformat(awaiting_since)
                                        timeout_minutes = 15
                                        countdown_label = ui.label("").classes("text-xs text-gray-500 mt-2")

                                        def update_countdown():
                                            now = datetime.now()
                                            elapsed = (now - started).total_seconds()
                                            remaining = (timeout_minutes * 60) - elapsed
                                            if remaining <= 0:
                                                countdown_label.text = "Auto-continuing now..."
                                            else:
                                                mins = int(remaining // 60)
                                                secs = int(remaining % 60)
                                                countdown_label.text = f"Auto-continues in {mins}:{secs:02d} if no response."

                                        update_countdown()
                                        ui.timer(1.0, update_countdown)
                                    except Exception:
                                        ui.label("Auto-continues after 15 minutes if no response.").classes("text-xs text-gray-500 mt-2")
                                else:
                                    ui.label("Auto-continues after 15 minutes if no response.").classes("text-xs text-gray-500 mt-2")

                # Build initial feedback panel
                build_feedback_panel()

                # Poll for status changes while job is active
                def check_status():
                    latest_job = job_manager.get_job(job_id)
                    if latest_job is None:
                        status_timer.deactivate()
                        return

                    # If status changed, refresh the entire page to update timeline, stats, etc.
                    if latest_job.status != current_status["value"]:
                        status_timer.deactivate()
                        ui.navigate.to(f"/job/{job_id}")

                # Only poll if job is still active
                if job_info.status in [JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.AWAITING_FEEDBACK]:
                    status_timer = ui.timer(5.0, check_status)  # Poll every 5 seconds

            else:
                ui.label("Knowledge graph not found").classes("text-gray-500")

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
                if job_info.status in [JobStatus.COMPLETED, JobStatus.FAILED]:
                    ui.label("Report generation failed").classes("text-red-500")
                else:
                    ui.label("Report will be available when job completes").classes("text-gray-500 italic")

    # Action buttons
    with ui.row().classes("mt-4 p-4"):
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


@ui.page("/billing")
def billing_page():
    """Billing and cost tracking page."""

    # Check authentication (skip if disabled)
    if not DISABLE_AUTH and not app.storage.user.get("authenticated", False):
        ui.navigate.to("/login")
        return

    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY - Billing").classes("text-h4")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"), icon="arrow_back")

    with ui.card().classes("w-full max-w-4xl mx-auto mt-8"):
        ui.label("Project Costs").classes("text-h5 mb-4")

        try:
            provider = get_provider()
            cost_info = provider.get_cost_info(lookback_hours=24)
            budget_check = provider.check_budget_limits()

            with ui.row().classes("w-full gap-8 mb-4"):
                # Total spend
                with ui.card().classes("flex-1"):
                    total_spend_display = f"${cost_info.total_spend_usd:.2f}" if cost_info.total_spend_usd is not None else "N/A"
                    ui.label(total_spend_display).classes("text-h3 text-primary")
                    ui.label("Total Spend").classes("text-subtitle2 text-grey")

                # Last 24h
                with ui.card().classes("flex-1"):
                    recent_spend_display = f"${cost_info.recent_spend_usd:.2f}" if cost_info.recent_spend_usd is not None else "N/A"
                    ui.label(recent_spend_display).classes("text-h3")
                    ui.label(f"Last {cost_info.recent_period_hours} Hours").classes("text-subtitle2 text-grey")

                # Budget remaining (if available)
                if cost_info.budget_remaining_usd is not None:
                    with ui.card().classes("flex-1"):
                        remaining_color = "text-positive" if cost_info.budget_remaining_usd > 10 else "text-warning"
                        ui.label(f"${cost_info.budget_remaining_usd:.2f}").classes(f"text-h3 {remaining_color}")
                        ui.label("Budget Remaining").classes("text-subtitle2 text-grey")

            # Provider info
            with ui.card().classes("w-full bg-gray-50"):
                ui.label("Provider Information").classes("text-subtitle2 font-bold mb-2")
                ui.label(f"Provider: {cost_info.provider_name}").classes("text-sm")
                if cost_info.data_lag_note:
                    ui.label(cost_info.data_lag_note).classes("text-sm text-grey-6")

            # Budget warnings/errors
            if budget_check and budget_check.get("errors"):
                with ui.card().classes("w-full bg-red-50 mt-4"):
                    ui.label("Budget Alerts").classes("text-subtitle2 font-bold text-red-800")
                    for error in budget_check["errors"]:
                        ui.label(f"⚠️ {error}").classes("text-sm text-red-600")
            elif budget_check and budget_check.get("warnings"):
                with ui.card().classes("w-full bg-yellow-50 mt-4"):
                    ui.label("Budget Warnings").classes("text-subtitle2 font-bold text-yellow-800")
                    for warning in budget_check["warnings"]:
                        ui.label(f"⚠️ {warning}").classes("text-sm text-yellow-600")

        except Exception as e:
            with ui.card().classes("w-full bg-yellow-50"):
                ui.label("Cost Tracking Unavailable").classes("text-subtitle2 font-bold")
                ui.label("Check provider configuration in .env").classes("text-sm text-gray-600")
                ui.label(f"Error: {e}").classes("text-xs text-gray-400 mt-2")


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

1. **Submit a Job**: Provide a research question and optionally upload data files
2. **Autonomous Discovery**: SHANDY runs for N iterations, analyzing data and searching literature
3. **View Results**: Track progress in the Timeline view, see key findings in Summary, and download the final Report

## Features

- **Autonomous**: Runs without human intervention
- **Domain-Agnostic**: Works for metabolomics, genomics, structural biology, and more
- **Literature-Grounded**: Searches PubMed for mechanistic insights
- **Progressive Disclosure**: See high-level summaries first, drill into details on demand
- **Downloadable Visualizations**: Export plots and the final report as PDF

## Supported Data Formats

SHANDY accepts various file types:

- **Tabular**: CSV, TSV, Excel (.xlsx), Parquet, JSON
- **Structures**: PDB, mmCIF (for structural biology)
- **Sequences**: FASTA
- **Images**: PNG, JPG

And many others. Data files are optional - you can also run literature-only investigations.

## Understanding Results

### Summary Tab
Shows key discoveries at a glance - the most important findings with statistical evidence.

### Timeline Tab
Chronological view of the investigation. Each iteration shows:
- What the agent investigated (plain-language summary)
- Visualizations generated (expandable)
- Literature searched (expandable with paper links)
- Findings recorded

### Report Tab
The final scientific report with:
- Executive summary
- Detailed findings with evidence
- Mechanistic interpretation
- Suggested follow-up experiments

Download as Markdown or PDF.

## Tips for Success

1. **Clear Research Question**: Be specific about what you want to discover
2. **Clean Data**: Ensure files are properly formatted. Provide a detailed explanation of the 
data file in your query if possible, including how it is formatted, any relevant details 
about the file (e.g. what the column headers signify), and how the file relates to the research question.
3. **Appropriate Iterations**: 10 is sufficient for many analyses. More iterations may help with more
complicated questions.

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
