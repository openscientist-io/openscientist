"""Job detail page with progressive disclosure UI.

Uses NiceGUI's websocket-based updates for real-time UI changes.
The page uses @ui.refreshable decorators to enable in-place updates
without full page reloads.
"""

import json
import logging
from collections import defaultdict

from nicegui import ui

from shandy.artifact_packager import create_artifacts_zip
from shandy.auth import get_current_user_id, require_auth
from shandy.database.session import AsyncSessionLocal
from shandy.job_manager import JobStatus, get_job_skills
from shandy.webapp_components.error_handler import get_user_friendly_error
from shandy.webapp_components.ui_components import (
    STATUS_COLORS,
    _inject_pubmed_badge_styles,
    render_delete_dialog,
    render_error_card,
    render_job_action_buttons,
    render_job_skills,
    render_justified_text,
    render_navigator,
    render_notifications_dialog,
    render_pmid_badge,
    render_share_dialog,
    render_stat_badges,
    render_text_with_pmid_links,
    render_thinking_status,
    transform_pmid_references,
)
from shandy.webapp_components.utils import (
    ClientGuard,
    guard_client,
    parse_transcript_actions,
    safe_run_javascript,
    setup_timer_cleanup,
)

logger = logging.getLogger(__name__)


def _load_knowledge_state(ks_path):
    """Load knowledge state from file, returning (data, error_message)."""
    if not ks_path.exists():
        return None, None
    try:
        with open(ks_path, encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse knowledge_state.json: %s", e)
        return None, "Knowledge state is being updated. Please refresh the page."


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

    # Set page title using short_title if available
    page_title = job_info.short_title or job_info.research_question[:50]
    if len(job_info.research_question) > 50 and not job_info.short_title:
        page_title += "..."
    ui.page_title(f"{page_title} - SHANDY")

    job_dir = job_manager.jobs_dir / job_id
    ks_path = job_dir / "knowledge_state.json"

    # Load initial knowledge state data
    ks_data, ks_load_error = _load_knowledge_state(ks_path)

    # Load initial skills count
    initial_skills = get_job_skills(job_id)
    initial_skills_count = len(initial_skills) if initial_skills else 0

    # State tracking for real-time updates
    _state = {
        "status": job_info.status,
        "iteration": ks_data.get("iteration", 0) if ks_data else 0,
        "findings_count": job_info.findings_count,
        "papers_count": len(ks_data.get("literature", [])) if ks_data else 0,
        "log_entries": len(ks_data.get("analysis_log", [])) if ks_data else 0,
        "agent_status": ks_data.get("agent_status") if ks_data else None,
        "skills_count": initial_skills_count,
    }

    # Track active timers for cleanup on disconnect
    _active_timers = setup_timer_cleanup()

    # Create reusable dialogs for Share, Delete, and Notifications actions
    share_dialog = render_share_dialog(job_id)
    delete_dialog = render_delete_dialog(
        job_id,
        job_manager,
        on_deleted=lambda: ui.navigate.to("/jobs"),
    )
    user_id = get_current_user_id()
    notifications_dialog = render_notifications_dialog(job_id, user_id)

    # Page header with navigation
    render_navigator()

    # Error message if failed (show prominently at top)
    if job_info.status == JobStatus.FAILED and job_info.error:
        error_info = get_user_friendly_error(job_info.error)
        render_error_card(error_info, job_info, job_dir)

    # Cancellation message if cancelled (show prominently at top)
    if job_info.status == JobStatus.CANCELLED:
        with ui.card().classes("w-full bg-orange-50 border border-orange-300 mb-4 p-4"):
            ui.label("Job Cancelled").classes("text-subtitle2 font-bold text-orange-800")
            reason = job_info.cancellation_reason or "No reason provided"
            ui.label(reason).classes("text-orange-700")

    # Warning if knowledge state couldn't be loaded (e.g., concurrent write)
    if ks_load_error:
        with ui.card().classes("w-full bg-yellow-50 border border-yellow-300 mb-4 p-4"):
            ui.label("Loading...").classes("text-subtitle2 font-bold text-yellow-800")
            ui.label(ks_load_error).classes("text-yellow-700")

    # 3-Tab Structure: Research Log (primary), Report, Chat
    with ui.tabs().classes("w-full") as tabs:
        timeline_tab = ui.tab("Research Log")
        report_tab = ui.tab("Report")
        chat_tab = ui.tab("Chat")

    with ui.tab_panels(tabs, value=timeline_tab).classes("w-full"):
        # ===== TIMELINE TAB (Primary View) =====
        with ui.tab_panel(timeline_tab):
            # Refreshable stats badges (updated via websocket)
            @ui.refreshable
            def render_job_stats():
                """Render job stats badges - refreshable for real-time updates."""
                from shandy.webapp_components.utils import is_client_connected

                if not is_client_connected():
                    return

                latest_job = job_manager.get_job(job_id)
                if latest_job is None:
                    return
                latest_ks, _ = _load_knowledge_state(ks_path)
                lit_count = len(latest_ks.get("literature", [])) if latest_ks else 0
                skills_count = len(get_job_skills(job_id))
                status_color = STATUS_COLORS.get(latest_job.status, "gray")

                # Build badge list - only show progress for active jobs
                badges = [
                    ("Status", latest_job.status.value, status_color),
                ]

                # Only show progress badge for non-terminal states
                if latest_job.status not in [
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                ]:
                    badges.append(
                        (
                            "Progress",
                            f"{latest_job.iterations_completed}/{latest_job.max_iterations}",
                            "blue",
                        )
                    )

                badges.extend(
                    [
                        ("Findings", latest_job.findings_count, "green"),
                        ("Papers", lit_count, "purple"),
                    ]
                )

                # Only show skills badge if there are skills
                if skills_count > 0:
                    badges.append(("Skills", skills_count, "cyan"))

                render_stat_badges(badges)

                # Show agent status if job is running and has a status message
                if latest_job.status == JobStatus.RUNNING and latest_ks:
                    agent_status = latest_ks.get("agent_status")
                    if agent_status:
                        with ui.element("div").classes("mt-2"):
                            render_thinking_status(agent_status)

            render_job_stats()

            # Research question with action buttons
            with ui.card().classes("w-full mb-4"):
                with ui.row().classes("w-full items-start justify-between"):
                    with ui.column().classes("flex-1"):
                        ui.label("Research Question").classes("text-subtitle2 font-bold")
                        ui.label(job_info.research_question).classes("text-lg")

                        # Show consensus answer if available (for completed jobs)
                        consensus = ks_data.get("consensus_answer") if ks_data else None
                        if consensus and job_info.status == JobStatus.COMPLETED:
                            with ui.element("div").classes(
                                "mt-3 p-3 bg-emerald-50 border-l-4 border-emerald-500 rounded"
                            ):
                                ui.label("Consensus Answer").classes(
                                    "text-xs font-bold text-emerald-700 uppercase tracking-wide"
                                )
                                ui.label(consensus).classes("text-emerald-900 mt-1")

                    render_job_action_buttons(
                        on_share=share_dialog.open,
                        on_delete=delete_dialog.open,
                        on_notifications=notifications_dialog.open,
                    )

            # Skills section (refreshable for real-time updates)
            @ui.refreshable
            def render_skills_section():
                """Render skills used section - refreshable for real-time updates."""
                from shandy.webapp_components.utils import is_client_connected

                if not is_client_connected():
                    return

                job_skills = get_job_skills(job_id)
                if job_skills:
                    with ui.expansion("Skills Used", icon="school").classes(
                        "w-full mb-4 border border-gray-200"
                    ):
                        render_job_skills(job_skills, show_content=True)

            render_skills_section()

            # Investigation Timeline (refreshable for real-time updates)
            ui.label("Investigation Timeline").classes("text-h6 font-bold mb-2")

            @ui.refreshable
            def render_timeline():
                """Render the investigation timeline - refreshable for real-time updates."""
                from shandy.webapp_components.utils import is_client_connected

                if not is_client_connected():
                    return

                # Reload knowledge state for latest data
                timeline_ks, _ = _load_knowledge_state(ks_path)
                latest_job = job_manager.get_job(job_id)
                if not timeline_ks or not latest_job:
                    ui.label("No investigation activity yet").classes("text-gray-500")
                    return

                # Get iteration summaries (agent-generated)
                timeline_iteration_summaries = {
                    s["iteration"]: {
                        "summary": s.get("summary", ""),
                        "strapline": s.get("strapline", ""),
                    }
                    for s in timeline_ks.get("iteration_summaries", [])
                }

                # Group analysis log by iteration
                timeline_by_iteration = defaultdict(list)
                for entry in timeline_ks.get("analysis_log", []):
                    timeline_by_iteration[entry["iteration"]].append(entry)

                # Get max iteration
                timeline_max_iter = timeline_ks.get("iteration", 1)

                if not timeline_by_iteration and not timeline_iteration_summaries:
                    ui.label("No investigation activity yet").classes("text-gray-500")
                    return

                with ui.scroll_area().classes("w-full h-[600px]"):
                    # Display in chronological order (oldest first)
                    # Don't show the current in-progress iteration if awaiting feedback
                    display_max = (
                        timeline_max_iter - 1
                        if latest_job.status == JobStatus.AWAITING_FEEDBACK
                        else timeline_max_iter
                    )
                    for iteration in range(1, display_max + 1):
                        entries = timeline_by_iteration.get(iteration, [])

                        # Check if this iteration is still in progress
                        is_in_progress = (
                            iteration == timeline_max_iter
                            and latest_job.status == JobStatus.RUNNING
                        )

                        # Get agent summary
                        iter_summary = timeline_iteration_summaries.get(iteration, {})
                        if isinstance(iter_summary, str):
                            strapline = ""
                            summary_text = iter_summary
                        else:
                            strapline = iter_summary.get("strapline", "")
                            summary_text = iter_summary.get("summary", "")

                        # Get counts from analysis_log
                        provenance_dir = job_dir / "provenance"
                        code_count = len([e for e in entries if e["action"] == "execute_code"])
                        search_count = len([e for e in entries if e["action"] == "search_pubmed"])
                        finding_count = len(
                            [e for e in entries if e["action"] == "update_knowledge_state"]
                        )

                        # Determine color based on outcome
                        border_class = "border-l-4 border-gray-300"
                        if finding_count > 0:
                            border_class = "border-l-4 border-green-500"
                        elif code_count > 0 or search_count > 0:
                            border_class = "border-l-4 border-blue-300"

                        # Header text
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
                                        ui.badge(f"{search_count} searches", color="purple").props(
                                            "outline"
                                        )
                                    if finding_count:
                                        ui.badge(f"{finding_count} findings", color="green")

                            # Container for lazy-loaded content
                            content_container = ui.column().classes("w-full")
                            content_loaded = {"value": False}

                            def load_iteration_content(
                                container,
                                loaded_flag,
                                iter_num=iteration,
                                iter_summary_text=summary_text,
                                iter_entries=entries,
                                iter_ks_data=timeline_ks,
                                iter_job_dir=job_dir,
                                iter_provenance_dir=provenance_dir,
                            ):
                                """Lazy load iteration content when expansion is opened."""
                                from shandy.webapp_components.utils import (
                                    is_client_connected,
                                )

                                if not is_client_connected():
                                    return

                                if loaded_flag["value"]:
                                    return
                                loaded_flag["value"] = True
                                container.clear()

                                with container:
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
                                                with ui.card().classes("w-full mb-2 bg-green-50"):
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
                                                        render_justified_text(
                                                            interpretation,
                                                            text_classes="text-sm text-gray-600 italic mt-1",
                                                        )

                                    # Load transcript lazily
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

                                                if "execute_code" in action.get("tool_name", ""):
                                                    card_class = (
                                                        "w-full mb-2 border-l-4 border-blue-300"
                                                    )
                                                elif "search_pubmed" in action.get("tool_name", ""):
                                                    card_class = (
                                                        "w-full mb-2 border-l-4 border-purple-300"
                                                    )
                                                elif "update_knowledge_state" in action.get(
                                                    "tool_name", ""
                                                ):
                                                    card_class = (
                                                        "w-full mb-2 border-l-4 border-green-300"
                                                    )
                                                else:
                                                    card_class = (
                                                        "w-full mb-2 border-l-4 border-gray-300"
                                                    )

                                                with ui.card().classes(card_class):
                                                    with ui.row().classes("items-center gap-2"):
                                                        ui.label(f"{status_icon} {desc}").classes(
                                                            "font-medium text-sm"
                                                        )
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
                                                    if result_text and len(str(result_text)) > 0:
                                                        result_str = str(result_text)
                                                        if len(result_str) > 200:
                                                            with ui.expansion(
                                                                "Result", icon="output"
                                                            ).classes("w-full mt-1"):
                                                                ui.code(
                                                                    result_str[:2000]
                                                                    + (
                                                                        "..."
                                                                        if len(result_str) > 2000
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
                                        for plot_file in sorted(iter_provenance_dir.glob("*.png")):
                                            metadata_file = plot_file.with_suffix(".json")
                                            if metadata_file.exists():
                                                with open(metadata_file, encoding="utf-8") as mf:
                                                    metadata = json.load(mf)
                                                if metadata.get("iteration") == iter_num:
                                                    iteration_plots.append((plot_file, metadata))

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
                                                            ).props("size=sm flat dense").classes(
                                                                "mt-2"
                                                            )

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
                                        e for e in iter_entries if e["action"] == "search_pubmed"
                                    ]
                                    if literature_entries:
                                        total_papers = sum(
                                            e.get("results_count", 0) for e in literature_entries
                                        )
                                        with ui.expansion(
                                            f"Literature searched ({total_papers} papers)",
                                            icon="article",
                                        ).classes("w-full mt-2"):
                                            for entry in literature_entries:
                                                query = entry.get("query", "")
                                                matching_papers = [
                                                    lit
                                                    for lit in iter_ks_data.get("literature", [])
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
                                                                abstract = paper.get("abstract", "")
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
                            expansion.on_value_change(
                                lambda e, cc=content_container, lf=content_loaded, fn=load_iteration_content: (
                                    fn(cc, lf) if e.value else None
                                )
                            )

            render_timeline()

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
                                    # Container for timer reference (allows closure access)
                                    timer_ref: list[ui.timer | None] = [None]

                                    @guard_client
                                    def update_countdown():
                                        now = datetime.now()
                                        elapsed = (now - started).total_seconds()
                                        remaining = (timeout_minutes * 60) - elapsed
                                        if remaining <= 0:
                                            countdown_label.text = "Auto-continuing now..."
                                            # Stop the timer when countdown is done
                                            if timer_ref[0]:
                                                timer_ref[0].deactivate()
                                        else:
                                            mins = int(remaining // 60)
                                            secs = int(remaining % 60)
                                            countdown_label.text = f"Auto-continues in {mins}:{secs:02d} if no response."

                                    update_countdown()
                                    timer_ref[0] = ui.timer(1.0, update_countdown)
                                    _active_timers.append(timer_ref[0])
                                except (ValueError, TypeError, OSError):
                                    ui.label(
                                        "Auto-continues after 15 minutes if no response."
                                    ).classes("text-xs text-gray-500 mt-2")
                            else:
                                ui.label("Auto-continues after 15 minutes if no response.").classes(
                                    "text-xs text-gray-500 mt-2"
                                )

            # Build initial feedback panel
            build_feedback_panel()

            # Poll for changes and refresh stats/timeline in real-time
            @guard_client
            def check_and_refresh():
                latest_job = job_manager.get_job(job_id)
                if latest_job is None:
                    stats_timer.deactivate()
                    return

                latest_ks, _ = _load_knowledge_state(ks_path)
                new_papers = len(latest_ks.get("literature", [])) if latest_ks else 0
                new_iter = latest_ks.get("iteration", 0) if latest_ks else 0
                new_log_entries = len(latest_ks.get("analysis_log", [])) if latest_ks else 0
                new_skills_count = len(get_job_skills(job_id))

                # Get agent status
                new_agent_status = latest_ks.get("agent_status") if latest_ks else None

                # Check if any stats changed (including agent status and skills)
                stats_changed = (
                    _state["findings_count"] != latest_job.findings_count
                    or _state["papers_count"] != new_papers
                    or _state["iteration"] != new_iter
                    or _state["agent_status"] != new_agent_status
                    or _state["skills_count"] != new_skills_count
                )

                # Refresh stats badges if changed
                if stats_changed:
                    _state["findings_count"] = latest_job.findings_count
                    _state["papers_count"] = new_papers
                    _state["iteration"] = new_iter
                    _state["agent_status"] = new_agent_status
                    # Refresh skills section if skills count changed
                    if _state["skills_count"] != new_skills_count:
                        _state["skills_count"] = new_skills_count
                        render_skills_section.refresh()
                    else:
                        _state["skills_count"] = new_skills_count
                    render_job_stats.refresh()

                # Check if timeline needs refresh (new log entries)
                if new_log_entries > _state["log_entries"]:
                    _state["log_entries"] = new_log_entries
                    render_timeline.refresh()

                # Status change requires different handling
                if latest_job.status != _state["status"]:
                    _state["status"] = latest_job.status

                    # Terminal states or feedback state need full page reload
                    # to update UI structure (show feedback panel, error card, etc.)
                    if latest_job.status in [
                        JobStatus.COMPLETED,
                        JobStatus.FAILED,
                        JobStatus.CANCELLED,
                        JobStatus.AWAITING_FEEDBACK,
                    ]:
                        stats_timer.deactivate()
                        ui.navigate.to(f"/job/{job_id}")
                    else:
                        # Just refresh stats for other transitions
                        render_job_stats.refresh()

            # Only poll if job is still active (including PENDING waiting to start)
            if job_info.status in [
                JobStatus.PENDING,
                JobStatus.RUNNING,
                JobStatus.QUEUED,
                JobStatus.AWAITING_FEEDBACK,
            ]:
                stats_timer = ui.timer(2.0, check_and_refresh)  # Poll every 2 seconds
                _active_timers.append(stats_timer)

        # ===== REPORT TAB =====
        with ui.tab_panel(report_tab):
            report_path = job_dir / "final_report.md"
            pdf_path = job_dir / "final_report.pdf"

            if report_path.exists():

                def download_artifacts():
                    """Create and download ZIP of all job artifacts."""
                    try:
                        zip_buffer = create_artifacts_zip(job_dir, job_id)
                        ui.download(
                            zip_buffer.getvalue(),
                            filename=f"{job_id}_artifacts.zip",
                        )
                    except Exception as e:
                        logger.error("Failed to create artifacts ZIP: %s", e)
                        ui.notify(f"Failed to create ZIP: {e}", type="negative")

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

                    # Artifacts ZIP (code, plots, provenance, etc.)
                    ui.button(
                        "Download All Artifacts",
                        on_click=download_artifacts,
                        icon="folder_zip",
                    ).props("color=accent outline")

                # Display markdown with PubMed badges for PMID references
                with open(report_path, encoding="utf-8") as f:
                    report_content = f.read()

                # Inject PubMed badge styles
                _inject_pubmed_badge_styles()

                # Transform PMID references to clickable badges
                report_with_badges = transform_pmid_references(report_content)
                ui.markdown(report_with_badges).classes("w-full")
            else:
                if job_info.status in [JobStatus.COMPLETED, JobStatus.FAILED]:
                    ui.label("Report generation failed").classes("text-red-500")
                else:
                    ui.label("Report will be available when job completes").classes(
                        "text-gray-500 italic"
                    )

        # ===== CHAT TAB =====
        with ui.tab_panel(chat_tab):
            if job_info.status != JobStatus.COMPLETED:
                ui.label("Chat will be available when the job completes.").classes(
                    "text-gray-500 italic"
                )
            else:
                # Import chat functions
                from uuid import UUID

                from shandy.job_chat import get_chat_history, send_chat_message

                job_uuid = UUID(job_id)

                # Chat styles - inject into head for proper application
                ui.add_head_html(
                    """
                <style>
                    .chat-bubble-user {
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        border-radius: 18px 18px 4px 18px;
                        padding: 12px 16px;
                        color: white;
                        max-width: 85%;
                        margin-left: auto;
                        word-wrap: break-word;
                        box-shadow: 0 2px 8px rgba(102, 126, 234, 0.3);
                    }
                    .chat-bubble-assistant {
                        background: linear-gradient(135deg, #e0f2fe 0%, #cffafe 100%);
                        border-radius: 18px 18px 18px 4px;
                        padding: 12px 16px;
                        color: #0c4a6e;
                        max-width: 85%;
                        word-wrap: break-word;
                        box-shadow: 0 2px 8px rgba(14, 116, 144, 0.15);
                        border: 1px solid #a5f3fc;
                    }
                    .chat-bubble-assistant .markdown-body {
                        background: transparent !important;
                    }
                    .chat-container {
                        background: linear-gradient(180deg, #fafbfc 0%, #f0f2f5 100%);
                        border-radius: 12px;
                        border: 1px solid #e1e4e8;
                    }
                    .chat-input-container {
                        background: white;
                        border-radius: 24px;
                        border: 2px solid #e1e4e8;
                        transition: border-color 0.2s, box-shadow 0.2s;
                        min-height: 48px;
                    }
                    .chat-input-container:focus-within {
                        border-color: #667eea;
                        box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
                    }
                    .chat-input-row {
                        align-items: center !important;
                    }
                    .chat-send-btn {
                        width: 48px !important;
                        height: 48px !important;
                        min-width: 48px !important;
                        min-height: 48px !important;
                    }
                </style>
                """
                )

                # Helper to play smooth sounds using Web Audio API
                def play_sound(sound_type: str):
                    # Generate smooth sine-wave sounds programmatically
                    safe_run_javascript(
                        f"""
                        (function() {{
                            try {{
                                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                                const type = '{sound_type}';

                                if (type === 'sound-send') {{
                                    // Soft ascending "pop" - two quick notes
                                    const osc1 = ctx.createOscillator();
                                    const gain1 = ctx.createGain();
                                    osc1.connect(gain1);
                                    gain1.connect(ctx.destination);
                                    osc1.frequency.setValueAtTime(600, ctx.currentTime);
                                    osc1.frequency.exponentialRampToValueAtTime(900, ctx.currentTime + 0.08);
                                    osc1.type = 'sine';
                                    gain1.gain.setValueAtTime(0, ctx.currentTime);
                                    gain1.gain.linearRampToValueAtTime(0.15, ctx.currentTime + 0.02);
                                    gain1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.12);
                                    osc1.start(ctx.currentTime);
                                    osc1.stop(ctx.currentTime + 0.15);
                                }} else if (type === 'sound-receive') {{
                                    // Pleasant two-tone chime
                                    const osc1 = ctx.createOscillator();
                                    const osc2 = ctx.createOscillator();
                                    const gain1 = ctx.createGain();
                                    const gain2 = ctx.createGain();
                                    osc1.connect(gain1);
                                    osc2.connect(gain2);
                                    gain1.connect(ctx.destination);
                                    gain2.connect(ctx.destination);
                                    osc1.type = 'sine';
                                    osc2.type = 'sine';
                                    osc1.frequency.value = 880;
                                    osc2.frequency.value = 1100;
                                    gain1.gain.setValueAtTime(0, ctx.currentTime);
                                    gain1.gain.linearRampToValueAtTime(0.12, ctx.currentTime + 0.02);
                                    gain1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
                                    gain2.gain.setValueAtTime(0, ctx.currentTime + 0.08);
                                    gain2.gain.linearRampToValueAtTime(0.1, ctx.currentTime + 0.1);
                                    gain2.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
                                    osc1.start(ctx.currentTime);
                                    osc2.start(ctx.currentTime + 0.08);
                                    osc1.stop(ctx.currentTime + 0.3);
                                    osc2.stop(ctx.currentTime + 0.35);
                                }} else if (type === 'sound-error') {{
                                    // Soft descending tone
                                    const osc = ctx.createOscillator();
                                    const gain = ctx.createGain();
                                    osc.connect(gain);
                                    gain.connect(ctx.destination);
                                    osc.type = 'sine';
                                    osc.frequency.setValueAtTime(440, ctx.currentTime);
                                    osc.frequency.exponentialRampToValueAtTime(280, ctx.currentTime + 0.2);
                                    gain.gain.setValueAtTime(0, ctx.currentTime);
                                    gain.gain.linearRampToValueAtTime(0.12, ctx.currentTime + 0.02);
                                    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.25);
                                    osc.start(ctx.currentTime);
                                    osc.stop(ctx.currentTime + 0.3);
                                }}
                            }} catch(e) {{}}
                        }})();
                    """
                    )

                # Chat container (full height)
                with (
                    ui.column()
                    .classes(
                        "w-full max-w-4xl mx-auto chat-container p-4 flex flex-col flex-nowrap"
                    )
                    .style("height: calc(100vh - 280px); min-height: 500px;")
                ):
                    # Header with SHANDY logo
                    with ui.row().classes("w-full items-center gap-3 mb-4 pb-2 border-b"):
                        ui.html(
                            """
                            <svg viewBox="0 0 100 100" width="28" height="28" xmlns="http://www.w3.org/2000/svg">
                                <path d="M22 18 Q50 18 50 40 Q50 60 78 60 Q78 82 50 82 Q22 82 22 60"
                                      fill="none" stroke="#0891b2" stroke-width="10" stroke-linecap="round"/>
                                <circle cx="22" cy="18" r="10" fill="#06b6d4"/>
                                <circle cx="78" cy="60" r="10" fill="#06b6d4"/>
                                <circle cx="22" cy="60" r="10" fill="#0e7490"/>
                            </svg>
                        """
                        )
                        ui.label("Research Assistant").classes("font-semibold text-gray-700")
                        ui.label("Discuss your findings").classes("text-sm text-gray-500 ml-auto")

                    # Messages area with unique class for JavaScript targeting
                    chat_scroll = (
                        ui.scroll_area()
                        .classes("w-full flex-grow px-2 chat-messages-scroll")
                        .style("min-height: 400px; max-height: calc(100vh - 350px);")
                    )

                    def scroll_chat_to_bottom():
                        """Scroll chat to bottom after new messages are added."""
                        safe_run_javascript(
                            """
                            setTimeout(() => {
                                const el = document.querySelector('.chat-messages-scroll .q-scrollarea__container');
                                if (el) el.scrollTop = el.scrollHeight;
                            }, 100);
                            """
                        )

                    # Scroll to bottom when chat tab becomes visible (uses MutationObserver
                    # because tab content is lazy-loaded and not in DOM until tab is selected)
                    ui.run_javascript(
                        """
                        if (!window._chatScrollObserver) {
                            window._chatScrollObserver = new MutationObserver(() => {
                                const el = document.querySelector('.chat-messages-scroll');
                                if (el && el.getBoundingClientRect().width > 0) {
                                    window._chatScrollObserver.disconnect();
                                    window._chatScrollObserver = null;
                                    const scroll = () => {
                                        const c = el.querySelector('.q-scrollarea__container');
                                        if (c) c.scrollTop = c.scrollHeight;
                                    };
                                    // Scroll multiple times as content renders
                                    [50, 150, 300].forEach(ms => setTimeout(scroll, ms));
                                }
                            });
                            window._chatScrollObserver.observe(document.body, {
                                childList: true, subtree: true, attributes: true
                            });
                        }
                        """
                    )

                    # Status indicator (hidden by default)
                    status_container = ui.element("div").classes("hidden")

                    with status_container:
                        render_thinking_status("Analyzing your message...")

                # Display welcome or existing messages
                async def render_messages():
                    """Render chat messages."""
                    guard = ClientGuard()
                    if not guard.is_connected:
                        return

                    try:
                        async with AsyncSessionLocal() as session:
                            messages = await get_chat_history(session, job_uuid)

                        # Re-check after await - client may have disconnected
                        if not guard.is_connected:
                            return

                        chat_scroll.clear()
                        with chat_scroll:
                            if not messages:
                                # Welcome message
                                with ui.column().classes("w-full items-center py-8"):
                                    ui.icon("chat_bubble_outline", size="xl").classes(
                                        "text-gray-300 mb-4"
                                    )
                                    ui.label("Start a conversation").classes(
                                        "text-lg font-medium text-gray-600"
                                    )
                                    ui.label("Ask questions about your research findings").classes(
                                        "text-sm text-gray-400 mb-4"
                                    )
                                    with ui.column().classes("gap-2"):
                                        for suggestion in [
                                            "What are the main findings?",
                                            "How strong is the evidence?",
                                            "What should I investigate next?",
                                        ]:
                                            with (
                                                ui.button(
                                                    suggestion,
                                                    on_click=lambda s=suggestion: quick_send(s),
                                                )
                                                .props("flat dense")
                                                .classes("text-indigo-600 normal-case")
                                            ):
                                                pass
                            else:
                                # Render message history
                                for msg in messages:
                                    render_message_bubble(msg.role, msg.content)

                        # Scroll to bottom after DOM has rendered
                        scroll_chat_to_bottom()
                    except Exception as e:
                        logger.error("Failed to load chat history: %s", e)

                def render_message_bubble(role: str, content: str):
                    """Render a single message bubble."""
                    if role == "user":
                        with ui.row().classes("w-full justify-end mb-3"):
                            with ui.element("div").classes("chat-bubble-user"):
                                ui.label(content).classes("text-sm")
                    else:
                        with ui.row().classes("items-start gap-2 mb-3"):
                            # Small SHANDY logo as avatar
                            ui.html(
                                """
                                <div style="width: 32px; height: 32px; background: #e0f2fe; border-radius: 50%; padding: 4px; flex-shrink: 0;">
                                    <svg viewBox="0 0 100 100" width="24" height="24" xmlns="http://www.w3.org/2000/svg">
                                        <path d="M22 18 Q50 18 50 40 Q50 60 78 60 Q78 82 50 82 Q22 82 22 60"
                                              fill="none" stroke="#0891b2" stroke-width="12" stroke-linecap="round"/>
                                        <circle cx="22" cy="18" r="10" fill="#06b6d4"/>
                                        <circle cx="78" cy="60" r="10" fill="#06b6d4"/>
                                        <circle cx="22" cy="60" r="10" fill="#0e7490"/>
                                    </svg>
                                </div>
                            """
                            )
                            with ui.element("div").classes("chat-bubble-assistant"):
                                ui.markdown(content).classes("text-sm")

                @guard_client
                async def quick_send(message: str):
                    """Send a quick suggestion message."""
                    chat_input.value = message
                    await send_message()

                # Load messages on page load
                _active_timers.append(ui.timer(0.1, render_messages, once=True))

                # Input area
                with ui.row().classes("w-full max-w-3xl mx-auto gap-3 mt-4 chat-input-row"):
                    with ui.element("div").classes(
                        "flex-grow chat-input-container flex items-center px-4"
                    ):
                        chat_input = (
                            ui.textarea(placeholder="Ask about your research...")
                            .classes("flex-grow")
                            .props("borderless dense rows=1 autogrow input-class='text-sm py-3'")
                        )

                    send_btn = (
                        ui.button(icon="send")
                        .props("round color=indigo size=md")
                        .classes("shadow-lg chat-send-btn")
                    )

                async def send_message():
                    """Send chat message to LLM."""
                    nonlocal status_container

                    guard = ClientGuard()
                    if not guard.is_connected:
                        return

                    message = chat_input.value
                    if not message or not message.strip():
                        return

                    # Play send sound
                    play_sound("sound-send")

                    # Clear input and disable - use JavaScript to ensure DOM is updated
                    chat_input.value = ""
                    guard.run_javascript(
                        "document.querySelector('textarea[placeholder=\"Ask about your research...\"]').value = ''"
                    )
                    send_btn.disable()

                    # Show user message immediately
                    with chat_scroll:
                        render_message_bubble("user", message.strip())

                    # Show typing indicator
                    status_container.classes(remove="hidden")

                    # Scroll to bottom after DOM has rendered
                    scroll_chat_to_bottom()

                    try:
                        async with AsyncSessionLocal() as session:
                            await send_chat_message(session, job_uuid, message.strip(), job_dir)

                        # Re-check after await - client may have disconnected
                        if not guard.is_connected:
                            return

                        # Hide typing indicator
                        status_container.classes(add="hidden")

                        # Play receive sound
                        play_sound("sound-receive")

                        # Reload all messages to show response
                        await render_messages()

                    except Exception as e:
                        logger.error("Chat error: %s", e)
                        if guard.is_connected:
                            status_container.classes(add="hidden")
                            play_sound("sound-error")
                            ui.notify(f"Error: {e}", type="negative")
                    finally:
                        if guard.is_connected:
                            send_btn.enable()
                            chat_input.run_method("focus")

                send_btn.on_click(send_message)

                # Enter to send (Shift+Enter for newline)
                chat_input.on(
                    "keydown.enter",
                    lambda e: send_message() if not e.args.get("shiftKey") else None,
                )
