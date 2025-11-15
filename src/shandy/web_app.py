"""
NiceGUI web interface for SHANDY.

Provides web UI for job submission, monitoring, and results viewing.
"""

import logging
import tempfile
from pathlib import Path
from typing import Optional

from nicegui import app, ui

from .job_manager import JobManager, JobStatus
from .cost_tracker import get_budget_info

logger = logging.getLogger(__name__)

# Global job manager
job_manager: Optional[JobManager] = None


def init_app(jobs_dir: Path = Path("jobs"), max_concurrent: int = 1):
    """Initialize the web application."""
    global job_manager
    job_manager = JobManager(jobs_dir=jobs_dir, max_concurrent=max_concurrent)

    # Add static file serving for job plots
    app.add_static_files('/jobs', str(jobs_dir))

    logger.info("Web app initialized")


# Global dict to store uploaded files per session
_uploaded_files = {}

@ui.page("/")
def index_page():
    """Homepage with job submission form."""

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

        # Check if files were uploaded
        if not _uploaded_files.get(session_id):
            ui.notify("Please upload at least one data file", type="negative")
            return

        # Generate job ID
        import uuid
        job_id = f"job_{uuid.uuid4().hex[:8]}"

        # Save uploaded files to temp location
        data_files = []
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

            # Navigate to jobs page
            ui.navigate.to("/jobs")

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
        upload = ui.upload(
            label="Upload Data Files (CSV)",
            multiple=True,
            auto_upload=True,
            on_upload=handle_upload
        ).classes("w-full")

        # Configuration
        with ui.row().classes("w-full"):
            max_iterations = ui.number(
                label="Max Iterations",
                value=10,
                min=5,
                max=100,
                step=5
            ).classes("flex-1")

            use_skills = ui.checkbox("Use Skills", value=True)

        # Budget info
        try:
            budget_info = get_budget_info()
            with ui.card().classes("w-full bg-blue-50"):
                ui.label("Budget Information").classes("text-subtitle2 font-bold")

                # Show remaining budget (may be None if no CBORG limit set)
                if budget_info['budget_remaining'] is not None:
                    ui.label(f"Remaining: ${budget_info['budget_remaining']:.2f}")
                else:
                    ui.label(f"Current Spend: ${budget_info['current_spend']:.2f}")

                ui.label(f"Per-Job Limit: ${budget_info['app_max_job_cost']:.2f}")
        except Exception as e:
            with ui.card().classes("w-full bg-yellow-50"):
                ui.label("Budget Information Unavailable").classes("text-subtitle2 font-bold")
                ui.label("Check ANTHROPIC_AUTH_TOKEN in .env").classes("text-sm text-gray-600")

        # Submit button
        ui.button("Start Discovery", on_click=submit_job).classes("w-full mt-4")

    # Quick links
    with ui.row().classes("w-full max-w-2xl mx-auto mt-4"):
        ui.button("View Jobs", on_click=lambda: ui.navigate.to("/jobs")).classes("flex-1")
        ui.button("Documentation", on_click=lambda: ui.navigate.to("/docs")).classes("flex-1")


@ui.page("/jobs")
def jobs_page():
    """Jobs list page."""

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
                "cost": f"${job.cost_usd:.2f}" if job.cost_usd else "N/A",
                "created": job.created_at[:19]  # Remove milliseconds
            }
            for job in jobs
        ]
        table.update()

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY - Jobs").classes("text-h4")
        with ui.row():
            ui.button("New Job", on_click=lambda: ui.navigate.to("/"))
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

        with ui.card():
            ui.label("Total Cost").classes("text-subtitle2")
            ui.label(f"${summary['total_cost_usd']:.2f}").classes("text-h4")

    # Jobs table
    table = ui.table(
        columns=[
            {"name": "job_id", "label": "Job ID", "field": "job_id", "align": "left"},
            {"name": "question", "label": "Research Question", "field": "question", "align": "left"},
            {"name": "status", "label": "Status", "field": "status", "align": "center"},
            {"name": "iterations", "label": "Iterations", "field": "iterations", "align": "center"},
            {"name": "findings", "label": "Findings", "field": "findings", "align": "center"},
            {"name": "cost", "label": "Cost", "field": "cost", "align": "right"},
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

    # Auto-refresh every 5 seconds
    ui.timer(5.0, refresh_jobs)


@ui.page("/job/{job_id}")
def job_detail_page(job_id: str):
    """Job detail page."""

    job_info = job_manager.get_job(job_id)

    if job_info is None:
        ui.label(f"Job {job_id} not found").classes("text-h5")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"))
        return

    # Page header
    with ui.header().classes("items-center justify-between"):
        ui.label(f"SHANDY - {job_id}").classes("text-h4")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"))

    # Job info cards
    with ui.row().classes("w-full gap-4 p-4"):
        with ui.card().classes("flex-1"):
            ui.label("Status").classes("text-subtitle2")

            # Status badge
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
            ui.linear_progress(job_info.iterations_completed / job_info.max_iterations)

        with ui.card().classes("flex-1"):
            ui.label("Findings").classes("text-subtitle2")
            ui.label(str(job_info.findings_count)).classes("text-h5")

        with ui.card().classes("flex-1"):
            ui.label("Cost").classes("text-subtitle2")
            cost_str = f"${job_info.cost_usd:.2f}" if job_info.cost_usd else "N/A"
            ui.label(cost_str).classes("text-h5")

    # Research question
    with ui.card().classes("w-full"):
        ui.label("Research Question").classes("text-subtitle2 font-bold")
        ui.label(job_info.research_question)

    # Error message if failed
    if job_info.status == JobStatus.FAILED and job_info.error:
        with ui.card().classes("w-full bg-red-50"):
            ui.label("Error").classes("text-subtitle2 font-bold text-red-800")
            ui.label(job_info.error).classes("text-red-600")

    # Tabs for different views
    with ui.tabs().classes("w-full") as tabs:
        findings_tab = ui.tab("Findings")
        hypotheses_tab = ui.tab("Hypotheses")
        literature_tab = ui.tab("Literature")
        plots_tab = ui.tab("Plots")
        log_tab = ui.tab("Analysis Log")
        report_tab = ui.tab("Final Report")

    job_dir = job_manager.jobs_dir / job_id
    kg_path = job_dir / "knowledge_graph.json"

    with ui.tab_panels(tabs, value=findings_tab).classes("w-full"):
        # Findings panel
        with ui.tab_panel(findings_tab):
            findings_container = ui.column().classes("w-full")

            def refresh_findings():
                findings_container.clear()
                with findings_container:
                    if kg_path.exists():
                        import json
                        with open(kg_path) as f:
                            kg = json.load(f)

                        if kg["findings"]:
                            for i, finding in enumerate(kg["findings"], 1):
                                with ui.card().classes("w-full"):
                                    ui.label(f"Finding {i}: {finding['title']}").classes("text-subtitle1 font-bold")
                                    ui.label(finding["evidence"]).classes("mt-2")
                                    # Show biological interpretation if available
                                    interpretation = finding.get("biological_interpretation") or finding.get("interpretation", "")
                                    if interpretation:
                                        ui.label(interpretation).classes("mt-2 text-gray-600")
                        else:
                            ui.label("No findings yet").classes("text-gray-500")
                    else:
                        ui.label("Knowledge graph not found").classes("text-gray-500")

            refresh_findings()
            ui.timer(5.0, refresh_findings)

        # Hypotheses panel
        with ui.tab_panel(hypotheses_tab):
            if kg_path.exists():
                with open(kg_path) as f:
                    kg = json.load(f)

                if kg["hypotheses"]:
                    for i, hyp in enumerate(kg["hypotheses"], 1):
                        with ui.card().classes("w-full"):
                            ui.label(f"H{i}: {hyp['hypothesis']}").classes("text-subtitle1 font-bold")
                            ui.label(f"Status: {hyp['status']}").classes("mt-2")
                            if hyp["status"] == "tested":
                                ui.label(f"Result: {hyp.get('result', 'N/A')}").classes("mt-2")
                else:
                    ui.label("No hypotheses yet").classes("text-gray-500")

        # Literature panel
        with ui.tab_panel(literature_tab):
            if kg_path.exists():
                with open(kg_path) as f:
                    kg = json.load(f)

                if kg["literature"]:
                    for i, lit in enumerate(kg["literature"], 1):
                        with ui.card().classes("w-full"):
                            ui.label(lit["title"]).classes("text-subtitle1 font-bold")
                            ui.label(f"PMID: {lit.get('pmid', 'N/A')}").classes("text-sm text-gray-600")
                            if "summary" in lit:
                                ui.label(lit["summary"]).classes("mt-2")
                else:
                    ui.label("No literature yet").classes("text-gray-500")

        # Plots panel
        with ui.tab_panel(plots_tab):
            plots_container = ui.column().classes("w-full")

            def refresh_plots():
                plots_container.clear()
                with plots_container:
                    plots_dir = job_dir / "plots"

                    if plots_dir.exists():
                        plot_files = sorted(plots_dir.glob("*.png"))

                        if plot_files:
                            ui.label(f"Generated {len(plot_files)} plot(s) showing what I'm thinking").classes("text-subtitle2 mb-4")

                            # Display plots in a grid
                            with ui.grid(columns=2).classes("w-full gap-4"):
                                for plot_file in plot_files:
                                    # Load metadata if available
                                    metadata_file = plot_file.with_suffix('.json')
                                    metadata = None
                                    if metadata_file.exists():
                                        import json
                                        with open(metadata_file) as f:
                                            metadata = json.load(f)

                                    with ui.card().classes("p-2"):
                                        # Plot header - make filename human-readable
                                        # Convert "amino_acid_analysis.png" -> "Amino Acid Analysis"
                                        plot_title = plot_file.stem.replace('_', ' ').title()

                                        if metadata and metadata.get('iteration') is not None:
                                            ui.label(f"Iteration {metadata['iteration']}: {plot_title}").classes("text-sm font-bold mb-2")
                                        else:
                                            ui.label(plot_title).classes("text-sm font-bold mb-2")

                                        # Claude's reasoning/description
                                        if metadata and metadata.get('description'):
                                            ui.label(f"🤔 {metadata['description']}").classes("text-sm text-blue-700 mb-2 italic")
                                        else:
                                            # If no metadata, show filename as description
                                            ui.label(f"📊 {plot_title}").classes("text-sm text-gray-600 mb-2 italic")

                                        # Display image - convert file path to URL path
                                        # jobs/job_123/plots/plot.png -> /jobs/job_123/plots/plot.png
                                        plot_url = f"/{plot_file}"
                                        ui.image(plot_url).classes("w-full")

                                        # Timestamp
                                        if metadata and metadata.get('timestamp'):
                                            ui.label(f"Created: {metadata['timestamp']}").classes("text-xs text-gray-500 mt-2")

                                        # Download button
                                        ui.button(
                                            "Download",
                                            on_click=lambda p=plot_file: ui.download(p.read_bytes(), filename=p.name),
                                            icon="download"
                                        ).props("size=sm flat")
                        else:
                            ui.label("No plots generated yet").classes("text-gray-500")
                    else:
                        ui.label("No plots directory found").classes("text-gray-500")

            refresh_plots()
            ui.timer(5.0, refresh_plots)

        # Analysis log panel
        with ui.tab_panel(log_tab):
            if kg_path.exists():
                with open(kg_path) as f:
                    kg = json.load(f)

                if kg["analysis_log"]:
                    with ui.scroll_area().classes("w-full h-96"):
                        for entry in kg["analysis_log"]:
                            with ui.card().classes("w-full mb-2"):
                                ui.label(f"Iteration {entry['iteration']}").classes("text-sm font-bold")
                                ui.label(entry["action"]).classes("text-sm")
                else:
                    ui.label("No analysis log yet").classes("text-gray-500")

        # Final report panel
        with ui.tab_panel(report_tab):
            report_path = job_dir / "final_report.md"

            if report_path.exists():
                with open(report_path) as f:
                    report_content = f.read()

                # Display markdown
                ui.markdown(report_content).classes("w-full")

                # Download button
                ui.button(
                    "Download Report",
                    on_click=lambda: ui.download(report_path.read_bytes(), filename=f"{job_id}_report.md")
                ).classes("mt-4")
            else:
                if job_info.status == JobStatus.COMPLETED:
                    ui.label("Report generation failed").classes("text-red-500")
                else:
                    ui.label("Report not yet available (job not complete)").classes("text-gray-500")

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


@ui.page("/docs")
def docs_page():
    """Documentation page."""

    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY - Documentation").classes("text-h4")
        ui.button("Back", on_click=lambda: ui.navigate.to("/"))

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
- **Domain-Agnostic**: Works for metabolomics, genomics, and more
- **Skills-Based**: Uses structured workflows for scientific rigor
- **Cost-Tracked**: Budget monitoring via CBORG API
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

SHANDY uses the CBORG API to track costs. Check your budget before starting large jobs.

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
        show=False  # Don't auto-open browser in Docker
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SHANDY Web Interface")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind to")
    parser.add_argument("--jobs-dir", default="jobs", help="Jobs directory")

    args = parser.parse_args()

    main(host=args.host, port=args.port, jobs_dir=Path(args.jobs_dir))
