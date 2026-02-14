"""Job detail page with progressive disclosure UI."""

import json
import logging
from collections import defaultdict

from nicegui import ui

from shandy.auth import require_auth
from shandy.job_manager import JobStatus
from shandy.webapp_components.error_handler import get_user_friendly_error
from shandy.webapp_components.ui_components import (
    STATUS_COLORS,
    render_delete_dialog,
    render_error_card,
    render_job_action_buttons,
    render_navigator,
    render_pmid_badge,
    render_share_dialog,
    render_stat_badges,
    render_text_with_pmid_links,
)
from shandy.webapp_components.utils.transcript_parser import parse_transcript_actions

logger = logging.getLogger(__name__)


@ui.page("/job/{job_id}")
@require_auth
def job_detail_page(job_id: str):
    """Job detail page with progressive disclosure UI."""
    # Import module to access global job_manager at runtime
    from shandy import web_app

    job_manager = web_app.get_job_manager()
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
            with open(ks_path, encoding="utf-8") as f:
                ks_data = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse knowledge_state.json for %s: %s", job_id, e)
            ks_load_error = "Knowledge state is being updated. Please refresh the page."

    # Create reusable dialogs for Share and Delete actions
    share_dialog = render_share_dialog(job_id)
    delete_dialog = render_delete_dialog(
        job_id,
        job_manager,
        on_deleted=lambda: ui.navigate.to("/jobs"),
    )

    # Page header with navigation
    render_navigator()

    # Error message if failed (show prominently at top)
    if job_info.status == JobStatus.FAILED and job_info.error:
        error_info = get_user_friendly_error(job_info.error)
        render_error_card(error_info, job_info, job_dir)

    # Warning if knowledge state couldn't be loaded (e.g., concurrent write)
    if ks_load_error:
        with ui.card().classes("w-full bg-yellow-50 border border-yellow-300 mb-4 p-4"):
            ui.label("Loading...").classes("text-subtitle2 font-bold text-yellow-800")
            ui.label(ks_load_error).classes("text-yellow-700")

    # 2-Tab Structure: Research Log (primary), Report
    with ui.tabs().classes("w-full") as tabs:
        timeline_tab = ui.tab("Research Log")
        report_tab = ui.tab("Report")

    with ui.tab_panels(tabs, value=timeline_tab).classes("w-full"):
        # ===== TIMELINE TAB (Primary View) =====
        with ui.tab_panel(timeline_tab):
            # Status badges (compact, mobile-friendly)
            status_color = STATUS_COLORS.get(job_info.status, "gray")
            lit_count = len(ks_data.get("literature", [])) if ks_data else 0
            render_stat_badges(
                [
                    ("Status", job_info.status.value, status_color),
                    (
                        "Progress",
                        f"{job_info.iterations_completed}/{job_info.max_iterations}",
                        "blue",
                    ),
                    ("Findings", job_info.findings_count, "green"),
                    ("Papers", lit_count, "purple"),
                ]
            )

            # Research question with action buttons
            with ui.card().classes("w-full mb-4"):
                with ui.row().classes("w-full items-start justify-between"):
                    with ui.column().classes("flex-1"):
                        ui.label("Research Question").classes("text-subtitle2 font-bold")
                        ui.label(job_info.research_question).classes("text-lg")
                    render_job_action_buttons(
                        on_share=share_dialog.open,
                        on_delete=delete_dialog.open,
                    )

            # Investigation Timeline
            ui.label("Investigation Timeline").classes("text-h6 font-bold mb-2")

            if ks_data:
                # Get iteration summaries (agent-generated) - includes strapline and full summary
                iteration_summaries = {
                    s["iteration"]: {
                        "summary": s.get("summary", ""),
                        "strapline": s.get("strapline", ""),
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
                        display_max = (
                            max_iter - 1
                            if job_info.status == JobStatus.AWAITING_FEEDBACK
                            else max_iter
                        )
                        for iteration in range(1, display_max + 1):
                            entries = by_iteration.get(iteration, [])

                            # Check if this iteration is still in progress
                            # max_iter is the CURRENT iteration being worked on
                            is_in_progress = (
                                iteration == max_iter and job_info.status == JobStatus.RUNNING
                            )

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
                            search_count = len(
                                [e for e in entries if e["action"] == "search_pubmed"]
                            )
                            finding_count = len(
                                [e for e in entries if e["action"] == "update_knowledge_state"]
                            )

                            # Determine color based on outcome
                            border_class = "border-l-4 border-gray-300"
                            if finding_count > 0:
                                border_class = "border-l-4 border-green-500"  # Found something!
                            elif code_count > 0 or search_count > 0:
                                border_class = "border-l-4 border-blue-300"  # Did work

                            # Use strapline for header if available, otherwise truncated summary
                            # Add "[in progress]" suffix for iterations still being worked on
                            if strapline:
                                header_text = (
                                    f"{strapline} [in progress]" if is_in_progress else strapline
                                )
                            elif summary_text:
                                truncated = (
                                    summary_text[:80] + "..."
                                    if len(summary_text) > 80
                                    else summary_text
                                )
                                header_text = (
                                    f"{truncated} [in progress]" if is_in_progress else truncated
                                )
                            elif is_in_progress:
                                header_text = "Investigation in progress..."
                            else:
                                header_text = "Completed"

                            with ui.expansion(icon="science").classes(
                                f"w-full mb-2 {border_class}"
                            ) as expansion:
                                # Custom header with badges using slot
                                with expansion.add_slot("header"):
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.label(f"Iteration {iteration}: {header_text}").classes(
                                            "font-medium"
                                        )
                                        if code_count:
                                            ui.badge(f"{code_count} analyses", color="blue").props(
                                                "outline"
                                            )
                                        if search_count:
                                            ui.badge(
                                                f"{search_count} searches",
                                                color="purple",
                                            ).props("outline")
                                        if finding_count:
                                            ui.badge(
                                                f"{finding_count} findings",
                                                color="green",
                                            )

                                # Container for lazy-loaded content
                                content_container = ui.column().classes("w-full")
                                content_loaded = {
                                    "value": False
                                }  # Track if content has been loaded

                                def load_iteration_content(
                                    container,
                                    loaded_flag,
                                    iter_num=iteration,
                                    iter_summary_text=summary_text,
                                    iter_entries=entries,
                                    iter_ks_data=ks_data,
                                    iter_job_dir=job_dir,
                                    iter_provenance_dir=provenance_dir,
                                ):
                                    """Lazy load iteration content when expansion is opened."""
                                    if loaded_flag["value"]:
                                        return  # Already loaded
                                    loaded_flag["value"] = True
                                    container.clear()

                                    with container:
                                        # Show full summary if available
                                        if iter_summary_text:
                                            with ui.expansion(
                                                "Summary", icon="summarize", value=True
                                            ).classes("w-full mt-2"):
                                                render_text_with_pmid_links(
                                                    iter_summary_text,
                                                    text_classes="text-sm text-gray-700",
                                                )

                                        # Show findings recorded
                                        iteration_findings = [
                                            f
                                            for f in iter_ks_data.get("findings", [])
                                            if f.get("iteration_discovered") == iter_num
                                        ]
                                        if iteration_findings:
                                            with ui.expansion(
                                                f"Findings ({len(iteration_findings)})",
                                                icon="lightbulb",
                                            ).classes("w-full mt-2"):
                                                for finding in iteration_findings:
                                                    with ui.card().classes(
                                                        "w-full mb-2 bg-green-50"
                                                    ):
                                                        ui.label(finding["title"]).classes(
                                                            "font-bold text-green-800"
                                                        )
                                                        render_text_with_pmid_links(
                                                            finding["evidence"],
                                                            text_classes="text-sm text-gray-700",
                                                        )
                                                        interpretation = finding.get(
                                                            "biological_interpretation"
                                                        ) or finding.get("interpretation", "")
                                                        if interpretation:
                                                            ui.label(interpretation).classes(
                                                                "text-sm text-gray-600 italic mt-1"
                                                            )

                                        # Load transcript lazily (this is the heavy part)
                                        transcript_path = (
                                            iter_provenance_dir / f"iter{iter_num}_transcript.json"
                                        )
                                        transcript_actions = []
                                        if transcript_path.exists():
                                            try:
                                                with open(transcript_path, encoding="utf-8") as tf:
                                                    transcript = json.load(tf)
                                                transcript_actions = parse_transcript_actions(
                                                    transcript
                                                )
                                            except (
                                                OSError,
                                                json.JSONDecodeError,
                                                KeyError,
                                            ) as e:
                                                logger.warning(
                                                    "Failed to load transcript for iter %d: %s",
                                                    iter_num,
                                                    e,
                                                )

                                        # Show actions from transcript
                                        if transcript_actions:
                                            with ui.expansion(
                                                f"Actions ({len(transcript_actions)})",
                                                icon="build",
                                            ).classes("w-full mt-2"):
                                                for action in transcript_actions:
                                                    success = action.get("success", True)
                                                    status_icon = "✅" if success else "❌"
                                                    desc = action.get(
                                                        "description",
                                                        action.get("short_name", "Unknown"),
                                                    )
                                                    tool_name = action.get("short_name", "")

                                                    if "execute_code" in action.get(
                                                        "tool_name", ""
                                                    ):
                                                        card_class = (
                                                            "w-full mb-2 border-l-4 border-blue-300"
                                                        )
                                                    elif "search_pubmed" in action.get(
                                                        "tool_name", ""
                                                    ):
                                                        card_class = "w-full mb-2 border-l-4 border-purple-300"
                                                    elif "update_knowledge_state" in action.get(
                                                        "tool_name", ""
                                                    ):
                                                        card_class = "w-full mb-2 border-l-4 border-green-300"
                                                    else:
                                                        card_class = (
                                                            "w-full mb-2 border-l-4 border-gray-300"
                                                        )

                                                    with ui.card().classes(card_class):
                                                        with ui.row().classes("items-center gap-2"):
                                                            ui.label(
                                                                f"{status_icon} {desc}"
                                                            ).classes("font-medium text-sm")
                                                            ui.badge(tool_name, color="gray").props(
                                                                "outline"
                                                            ).classes("text-xs")

                                                        inp = action.get("input", {})
                                                        if "execute_code" in action.get(
                                                            "tool_name", ""
                                                        ) and inp.get("code"):
                                                            with ui.expansion(
                                                                "Code", icon="code"
                                                            ).classes("w-full mt-1"):
                                                                ui.code(
                                                                    inp["code"],
                                                                    language="python",
                                                                ).classes("text-xs")

                                                        if "search_pubmed" in action.get(
                                                            "tool_name", ""
                                                        ) and inp.get("query"):
                                                            ui.label(
                                                                f'Query: "{inp["query"]}"'
                                                            ).classes("text-xs text-gray-600 mt-1")

                                                        result_text = action.get("result", "")
                                                        if (
                                                            result_text
                                                            and len(str(result_text)) > 0
                                                        ):
                                                            result_str = str(result_text)
                                                            if len(result_str) > 200:
                                                                with ui.expansion(
                                                                    "Result",
                                                                    icon="output",
                                                                ).classes("w-full mt-1"):
                                                                    ui.code(
                                                                        result_str[:2000]
                                                                        + (
                                                                            "..."
                                                                            if len(result_str)
                                                                            > 2000
                                                                            else ""
                                                                        ),
                                                                        language="text",
                                                                    ).classes("text-xs")
                                                            elif not success:
                                                                ui.label(result_str).classes(
                                                                    "text-xs text-red-600 mt-1"
                                                                )

                                        # Show plots from this iteration
                                        if iter_provenance_dir.exists():
                                            iteration_plots = []
                                            for plot_file in sorted(
                                                iter_provenance_dir.glob("*.png")
                                            ):
                                                metadata_file = plot_file.with_suffix(".json")
                                                if metadata_file.exists():
                                                    with open(
                                                        metadata_file, encoding="utf-8"
                                                    ) as mf:
                                                        metadata = json.load(mf)
                                                    if metadata.get("iteration") == iter_num:
                                                        iteration_plots.append(
                                                            (plot_file, metadata)
                                                        )

                                            if iteration_plots:
                                                with ui.expansion(
                                                    f"Visualizations ({len(iteration_plots)})",
                                                    icon="insert_chart",
                                                ).classes("w-full mt-2"):
                                                    with ui.grid(columns=2).classes("w-full gap-2"):
                                                        for (
                                                            plot_file,
                                                            metadata,
                                                        ) in iteration_plots:
                                                            plot_title = plot_file.stem.replace(
                                                                "_", " "
                                                            ).title()
                                                            description = metadata.get(
                                                                "description", ""
                                                            )

                                                            with ui.card().classes("p-2"):
                                                                ui.label(plot_title).classes(
                                                                    "text-sm font-bold"
                                                                )
                                                                if description:
                                                                    ui.label(description).classes(
                                                                        "text-xs text-blue-700 italic"
                                                                    )
                                                                plot_url = f"/{plot_file}"
                                                                ui.image(plot_url).classes("w-full")

                                                                ui.button(
                                                                    "Download",
                                                                    on_click=lambda p=plot_file: (
                                                                        ui.download(
                                                                            p.read_bytes(),
                                                                            filename=p.name,
                                                                        )
                                                                    ),
                                                                    icon="download",
                                                                ).props(
                                                                    "size=sm flat dense"
                                                                ).classes("mt-2")

                                                                plot_code = metadata.get("code")
                                                                if plot_code:
                                                                    with ui.expansion(
                                                                        "View code",
                                                                        icon="code",
                                                                    ).classes("w-full mt-1"):
                                                                        ui.code(
                                                                            plot_code,
                                                                            language="python",
                                                                        ).classes("text-xs")

                                        # Show literature searched
                                        literature_entries = [
                                            e
                                            for e in iter_entries
                                            if e["action"] == "search_pubmed"
                                        ]
                                        if literature_entries:
                                            total_papers = sum(
                                                e.get("results_count", 0)
                                                for e in literature_entries
                                            )
                                            with ui.expansion(
                                                f"Literature searched ({total_papers} papers)",
                                                icon="article",
                                            ).classes("w-full mt-2"):
                                                for entry in literature_entries:
                                                    query = entry.get("query", "")
                                                    matching_papers = [
                                                        lit
                                                        for lit in iter_ks_data.get(
                                                            "literature", []
                                                        )
                                                        if lit.get("search_query") == query
                                                        and lit.get("retrieved_at_iteration")
                                                        == iter_num
                                                    ]
                                                    if matching_papers:
                                                        with ui.expansion(
                                                            f'"{query}" ({len(matching_papers)} papers)'
                                                        ).classes("w-full"):
                                                            for paper in matching_papers:
                                                                with ui.card().classes(
                                                                    "w-full mb-1 p-2"
                                                                ):
                                                                    ui.label(
                                                                        paper.get(
                                                                            "title",
                                                                            "Untitled",
                                                                        )
                                                                    ).classes("text-sm font-bold")
                                                                    pmid = paper.get("pmid", "")
                                                                    if pmid:
                                                                        render_pmid_badge(pmid)
                                                                    abstract = paper.get(
                                                                        "abstract",
                                                                        "",
                                                                    )
                                                                    if abstract:
                                                                        ui.label(
                                                                            abstract[:200] + "..."
                                                                            if len(abstract) > 200
                                                                            else abstract
                                                                        ).classes(
                                                                            "text-xs text-gray-600 mt-1"
                                                                        )
                                                    else:
                                                        ui.label(f'"{query}" (0 results)').classes(
                                                            "text-sm text-gray-400 italic"
                                                        )

                                # Show loading placeholder initially
                                with content_container:
                                    ui.label("Click to load details...").classes(
                                        "text-sm text-gray-400 italic"
                                    )

                                # Trigger lazy load when expansion is opened
                                # NOTE: Must capture load_iteration_content with default arg, otherwise
                                # all callbacks will use the last iteration's function (closure bug)
                                expansion.on_value_change(
                                    lambda e, cc=content_container, lf=content_loaded, fn=load_iteration_content: (
                                        fn(cc, lf) if e.value else None
                                    )
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
                            with open(ks_path, encoding="utf-8") as f:
                                latest_ks = json.load(f)
                            next_iter = latest_ks.get("iteration", 1)
                        # The completed iteration is the previous one
                        completed_iter = next_iter - 1 if next_iter > 1 else 1

                        # Get awaiting_feedback_since from config
                        config_path = job_dir / "config.json"
                        if config_path.exists():
                            with open(config_path, encoding="utf-8") as f:
                                cfg = json.load(f)
                            awaiting_since = cfg.get("awaiting_feedback_since")

                        with feedback_container:
                            with ui.card().classes(
                                "w-full mt-2 bg-yellow-50 border-2 border-yellow-400 p-6"
                            ):
                                ui.label(
                                    f"Iteration {completed_iter} Complete - Awaiting Your Input"
                                ).classes("text-h6 font-bold text-yellow-800")
                                ui.label(
                                    "Provide guidance for the next iteration, or continue without feedback."
                                ).classes("text-sm text-gray-700 mb-4")

                                feedback_input = ui.textarea(
                                    label="Your Feedback (optional)",
                                    placeholder="e.g., Focus on metabolic pathways, or investigate the correlation with gene X...",
                                ).classes("w-full")

                                with ui.row().classes("w-full gap-2 mt-2"):

                                    def submit_feedback(fi=feedback_input, ci=completed_iter):
                                        from shandy.knowledge_state import (
                                            KnowledgeState,
                                        )

                                        ks = KnowledgeState.load(job_dir / "knowledge_state.json")
                                        if fi.value.strip():
                                            ks.add_feedback(fi.value.strip(), ci)
                                            ks.save(job_dir / "knowledge_state.json")
                                        # Set status back to running to signal continue
                                        with open(job_dir / "config.json", encoding="utf-8") as f:
                                            cfg = json.load(f)
                                        cfg["status"] = "running"
                                        with open(
                                            job_dir / "config.json",
                                            "w",
                                            encoding="utf-8",
                                        ) as f:
                                            json.dump(cfg, f, indent=2)
                                        ui.notify(
                                            "Continuing to next iteration",
                                            type="positive",
                                        )
                                        ui.navigate.to(f"/job/{job_id}")

                                    ui.button(
                                        "Submit & Continue",
                                        on_click=submit_feedback,
                                        icon="send",
                                    ).props("color=primary")
                                    ui.button(
                                        "Continue Without Feedback",
                                        on_click=submit_feedback,
                                        icon="arrow_forward",
                                    ).props("color=secondary outline")

                                # Countdown timer
                                if awaiting_since:
                                    from datetime import datetime

                                    try:
                                        started = datetime.fromisoformat(awaiting_since)
                                        timeout_minutes = 15
                                        countdown_label = ui.label("").classes(
                                            "text-xs text-gray-500 mt-2"
                                        )

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
                                    except (ValueError, TypeError, OSError):
                                        ui.label(
                                            "Auto-continues after 15 minutes if no response."
                                        ).classes("text-xs text-gray-500 mt-2")
                                else:
                                    ui.label(
                                        "Auto-continues after 15 minutes if no response."
                                    ).classes("text-xs text-gray-500 mt-2")

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
                if job_info.status in [
                    JobStatus.RUNNING,
                    JobStatus.QUEUED,
                    JobStatus.AWAITING_FEEDBACK,
                ]:
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
                        on_click=lambda: ui.download(
                            report_path.read_bytes(), filename=f"{job_id}_report.md"
                        ),
                        icon="download",
                    ).props("color=secondary outline")

                    if pdf_path.exists():
                        ui.button(
                            "Download PDF",
                            on_click=lambda: ui.download(
                                pdf_path.read_bytes(), filename=f"{job_id}_report.pdf"
                            ),
                            icon="picture_as_pdf",
                        ).props("color=primary")
                    else:
                        ui.button("PDF Unavailable", icon="picture_as_pdf").props(
                            "color=grey outline disabled"
                        )

                # Display markdown
                with open(report_path, encoding="utf-8") as f:
                    report_content = f.read()
                ui.markdown(report_content).classes("w-full")
            else:
                if job_info.status in [JobStatus.COMPLETED, JobStatus.FAILED]:
                    ui.label("Report generation failed").classes("text-red-500")
                else:
                    ui.label("Report will be available when job completes").classes(
                        "text-gray-500 italic"
                    )

    # Action buttons (Cancel only - Delete is in the Research Question card)
    if job_info.status in [JobStatus.RUNNING, JobStatus.QUEUED]:
        with ui.row().classes("mt-4 p-4"):
            ui.button("Cancel Job", on_click=lambda: cancel_job(job_id), color="red")

    def cancel_job(jid):
        """Cancel the job."""
        try:
            job_manager.cancel_job(jid)
            ui.notify(f"Job {jid} cancelled", type="positive")
            ui.navigate.to("/jobs")
        except (ValueError, OSError) as e:
            ui.notify(f"Error cancelling job: {e}", type="negative")
