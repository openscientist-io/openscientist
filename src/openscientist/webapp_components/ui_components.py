"""
Reusable UI components for OpenScientist web interface.

Provides UI rendering functions for error displays, status badges,
page headers, and other common interface elements.
"""

import html
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from nicegui import app, ui
from sqlalchemy import or_, select, update

from openscientist.auth import get_current_user_id
from openscientist.database.models import Job, JobShare, User
from openscientist.database.rls import set_current_user
from openscientist.database.session import get_admin_session, get_session_ctx
from openscientist.job.types import JobInfo, JobStatus
from openscientist.ntfy import ensure_user_has_topic, get_subscription_url, send_notification

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from openscientist.job_manager import JobManager


OPENSCIENTIST_DOCS_URL = "/docs"
OPENSCIENTIST_GITHUB_URL = "https://github.com/openscientist-io/openscientist"
OPENSCIENTIST_RELEASE_URL = "https://github.com/openscientist-io/openscientist/releases/latest"
OPENSCIENTIST_PAPER_URL: str | None = None


def get_project_resource_links(*, include_docs: bool = True) -> list[tuple[str, str]]:
    """Return the verified project links we expose in the UI."""
    links: list[tuple[str, str]] = []
    if include_docs:
        links.append(("Docs", OPENSCIENTIST_DOCS_URL))
    links.append(("GitHub", OPENSCIENTIST_GITHUB_URL))
    if OPENSCIENTIST_PAPER_URL:
        links.append(("Paper", OPENSCIENTIST_PAPER_URL))
    links.append(("Latest Release", OPENSCIENTIST_RELEASE_URL))
    return links


def render_project_resource_links(*, include_docs: bool = True) -> None:
    """Render a compact row of project resource links."""
    with ui.column().classes("w-full items-center gap-2"):
        ui.label("Resources").classes("text-xs font-semibold uppercase tracking-wide text-cyan-600")
        with ui.row().classes("w-full justify-center gap-2 flex-wrap"):
            for label, target in get_project_resource_links(include_docs=include_docs):
                ui.link(label, target=target, new_tab=target.startswith("http")).classes(
                    "no-underline rounded-full border border-cyan-200 bg-cyan-50 px-3 py-1 "
                    "text-sm font-medium text-cyan-700 hover:bg-cyan-100"
                )


def format_relative_time(dt: datetime | None) -> str:
    """
    Format datetime as relative time (e.g., '2 hours ago').

    Args:
        dt: Datetime to format, or None

    Returns:
        Human-readable relative time string, or '-' if dt is None
    """
    if dt is None:
        return "-"

    # Ensure dt is timezone-aware (assume UTC if naive)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    now = datetime.now(UTC)
    delta = now - dt

    seconds = delta.total_seconds()

    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        minutes = int(seconds / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = int(seconds / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if seconds < 604800:
        days = int(seconds / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    if seconds < 2592000:
        weeks = int(seconds / 604800)
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    months = int(seconds / 2592000)
    return f"{months} month{'s' if months != 1 else ''} ago"


def render_skill_name_slot() -> str:
    """
    Generate Quasar table slot template for skill name column with clickable link.

    Returns slot template string that renders skill names as clickable links
    navigating to the skill detail page.

    Returns:
        Quasar slot template string
    """
    return r"""
        <q-td :props="props">
            <span
                class="skill-name-link"
                style="color:#0891b2;cursor:pointer;font-weight:500;"
                @click="$parent.$emit('view-skill', {category: props.row.category, slug: props.row.slug})"
            >
                {{ props.row.name }}
            </span>
            <div v-if="props.row.description" class="text-caption text-grey-7" style="max-width:400px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                {{ props.row.description }}
            </div>
        </q-td>
    """


# Type alias for async callbacks
AsyncCallback = Callable[[dict[str, Any]], Awaitable[None]]

# Status color mappings
STATUS_COLORS = {
    JobStatus.PENDING: "gray",
    JobStatus.QUEUED: "blue",
    JobStatus.RUNNING: "teal",
    JobStatus.GENERATING_REPORT: "teal",
    JobStatus.COMPLETED: "green",
    JobStatus.FAILED: "red",
    JobStatus.CANCELLED: "gray",
    JobStatus.AWAITING_FEEDBACK: "orange",
}

# Status icons (unicode/emoji)
STATUS_ICONS = {
    JobStatus.PENDING: "○",
    JobStatus.QUEUED: "⟳",
    JobStatus.RUNNING: "▶",
    JobStatus.GENERATING_REPORT: "⟳",
    JobStatus.COMPLETED: "✓",
    JobStatus.FAILED: "✗",
    JobStatus.CANCELLED: "⊗",
    JobStatus.AWAITING_FEEDBACK: "⏸",
}

# Category color mappings for skills
CATEGORY_COLORS: dict[str, str] = {
    "analysis": "blue",
    "methodology": "purple",
    "statistics": "green",
    "biology": "teal",
    "chemistry": "orange",
    "bioinformatics": "cyan",
    "machine-learning": "indigo",
    "data-science": "violet",
    "genomics": "pink",
    "metabolomics": "amber",
    "proteomics": "lime",
}


def get_category_color(category: str) -> str:
    """
    Get the color for a skill category.

    Args:
        category: The category name (case-insensitive)

    Returns:
        Color string for use with Quasar/Tailwind (e.g., "blue", "purple")
    """
    return CATEGORY_COLORS.get(category.lower(), "gray")


def _get_pubmed_badge_html(pmid: str) -> str:
    """
    Generate HTML for a PubMed badge with logo and PMID.

    Creates an inline badge element with:
    - PubMed logo (inline SVG)
    - PMID number
    - Tooltip explaining the link
    - Opens in new tab (via CSS class, handled by injected script)

    Args:
        pmid: The PubMed ID number

    Returns:
        HTML string for the badge element
    """
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    tooltip = f"Visit PubMed page for PMID {pmid}"

    # PubMed-style icon: stylized "P" in a rounded square
    # Using a simple, clean design that's recognizable at small sizes
    # Note: Explicit width/height/style attributes ensure proper sizing even if CSS not loaded
    pubmed_icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" '
        'width="14" height="14" class="pubmed-icon" '
        'style="display:inline !important;width:14px;height:14px;min-width:14px;'
        'min-height:14px;flex-shrink:0;vertical-align:middle;margin-right:3px;">'
        '<rect x="1" y="1" width="14" height="14" rx="2" fill="#326599"/>'
        '<text x="8" y="12" text-anchor="middle" '
        'style="font-size:11px;font-weight:bold;font-family:Arial,sans-serif;fill:white;">'
        "P</text></svg>"
    )

    return (
        f'<a href="{url}" rel="noopener noreferrer" '
        f'title="{tooltip}" class="pubmed-badge" '
        f'style="display:inline-flex;align-items:center;text-decoration:none;'
        f'white-space:nowrap;">'
        f"{pubmed_icon}{html.escape(pmid)}</a>"
    )


def _inject_pubmed_badge_styles() -> None:
    """Inject CSS and JS for PubMed badges into page head (idempotent)."""
    # Using add_head_html with shared=True ensures this is only added once per client
    ui.add_head_html(
        """
        <style>
        .pubmed-badge {
            display: inline-flex;
            align-items: center;
            padding: 1px 6px 1px 4px;
            margin: 0 2px;
            background-color: #e8f4f8;
            border: 1px solid #326599;
            border-radius: 4px;
            text-decoration: none;
            color: #326599;
            font-size: 0.85em;
            font-weight: 500;
            transition: background-color 0.2s;
            white-space: nowrap;
        }
        .pubmed-badge:hover {
            background-color: #cce5ed;
        }
        .pubmed-icon {
            width: 14px;
            height: 14px;
            vertical-align: middle;
            margin-right: 3px;
        }
        </style>
        <script>
        // Make all PubMed links open in new tab (event delegation)
        if (!window._pubmedClickHandlerAdded) {
            window._pubmedClickHandlerAdded = true;
            document.addEventListener('click', function(e) {
                var link = e.target.closest('.pubmed-badge');
                if (link) {
                    e.preventDefault();
                    window.open(link.href, '_blank', 'noopener,noreferrer');
                }
            });
        }
        </script>
        """,
        shared=True,
    )


def _inject_job_id_badge_styles() -> None:
    """Inject CSS and JS for job ID badges into page head (idempotent)."""
    ui.add_head_html(
        """
        <style>
        .job-id-badge {
            display: inline-flex;
            align-items: center;
            padding: 2px 8px;
            background-color: #f3f4f6;
            border: 1px solid #9ca3af;
            border-radius: 4px;
            text-decoration: none;
            color: #374151;
            font-size: 0.8em;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-weight: 500;
            transition: background-color 0.2s, border-color 0.2s;
            cursor: pointer;
            white-space: nowrap;
        }
        .job-id-badge:hover {
            background-color: #e5e7eb;
            border-color: #6b7280;
            color: #111827;
        }
        .job-id-badge .job-icon {
            width: 14px;
            height: 14px;
            margin-right: 4px;
            fill: currentColor;
        }
        </style>
        <script>
        // Make job ID badges navigate on click (event delegation)
        if (!window._jobIdClickHandlerAdded) {
            window._jobIdClickHandlerAdded = true;
            document.addEventListener('click', function(e) {
                var badge = e.target.closest('.job-id-badge');
                if (badge && badge.dataset.jobId) {
                    e.preventDefault();
                    window.location.href = '/job/' + badge.dataset.jobId;
                }
            });
        }
        </script>
        """,
        shared=True,
    )


def _get_job_id_badge_html(job_id: str, truncate: bool = True) -> str:
    """
    Generate HTML for a job ID badge.

    Args:
        job_id: The job UUID
        truncate: If True, show only last 8 characters of UUID

    Returns:
        HTML string for the badge element
    """
    display_id = job_id[-8:] if truncate and len(job_id) > 8 else job_id
    tooltip = f"View job {job_id}"

    # Simple work/document icon
    # Note: Explicit width/height/style attributes ensure proper sizing even if CSS not loaded
    job_icon = (
        '<svg class="job-icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        'width="14" height="14" '
        'style="display:inline !important;width:14px;height:14px;min-width:14px;'
        'min-height:14px;flex-shrink:0;">'
        '<path d="M20 6h-4V4c0-1.1-.9-2-2-2h-4c-1.1 0-2 .9-2 2v2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-6 0h-4V4h4v2z"/>'
        "</svg>"
    )

    return (
        f'<span class="job-id-badge" data-job-id="{html.escape(job_id)}" '
        f'title="{tooltip}" '
        f'style="display:inline-flex;align-items:center;white-space:nowrap;">'
        f"{job_icon}{html.escape(display_id)}</span>"
    )


def render_job_id_badge(job_id: str, truncate: bool = True) -> None:
    """
    Render a job ID as a clickable badge.

    Creates an inline badge element with work icon, truncated job ID,
    tooltip, and click handler that navigates to the job detail page.

    Args:
        job_id: The job UUID
        truncate: If True, show only last 8 characters of UUID (default True)
    """
    _inject_job_id_badge_styles()
    badge_html = _get_job_id_badge_html(job_id, truncate)
    ui.html(badge_html)


def render_job_id_slot(field_name: str = "job_id") -> str:
    """
    Generate Quasar table slot template for job ID column with clickable badges.

    Returns slot template string that renders job IDs as clickable badges
    linking to the job detail page.

    Args:
        field_name: The row field containing the job ID (default: "job_id")

    Returns:
        Quasar slot template string
    """
    return f"""
        <q-td :props="props">
            <span
                class="job-id-badge"
                :data-job-id="props.row.{field_name}"
                :title="'View job ' + props.row.{field_name}"
                style="display:inline-flex;align-items:center;padding:2px 8px;background-color:#f3f4f6;border:1px solid #9ca3af;border-radius:4px;text-decoration:none;color:#374151;font-size:0.8em;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-weight:500;cursor:pointer;white-space:nowrap;"
                @click="$parent.$emit('view-job', props.row.{field_name})"
            >
                <svg width="14" height="14" style="display:inline !important;width:14px;height:14px;min-width:14px;min-height:14px;margin-right:4px;fill:currentColor;flex-shrink:0;" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
                    <path d="M20 6h-4V4c0-1.1-.9-2-2-2h-4c-1.1 0-2 .9-2 2v2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm-6 0h-4V4h4v2z"/>
                </svg>
                {{{{ props.row.{field_name}.slice(-8) }}}}
            </span>
        </q-td>
    """


def render_pmid_badge(pmid: str) -> None:
    """
    Render a single PMID as a clickable PubMed badge.

    Creates an inline badge element with PubMed logo, PMID number,
    tooltip, and link that opens in a new tab.

    Use this for standalone PMID display (e.g., in literature lists).
    For PMIDs embedded in text, use render_text_with_pmid_links() instead.

    Args:
        pmid: The PubMed ID number (just the numeric part)
    """
    # Inject CSS/JS for badges into page head
    _inject_pubmed_badge_styles()

    # Render badge as inline HTML
    badge_html = _get_pubmed_badge_html(pmid)
    ui.html(badge_html)


def render_text_with_pmid_links(
    text: str,
    text_classes: str = "text-sm text-gray-700",
) -> None:
    """
    Render text with PMID references converted to clickable badges.

    Parses text for PMID patterns and renders them as clickable PubMed badges
    with logo, tooltip, and link to PubMed. Supports both single PMIDs and
    comma-separated lists.

    Patterns matched:
    - "PMID: 12345678"
    - "PMID 12345678"
    - "PMID: 12345678, 87654321, 11111111"

    Args:
        text: The text containing PMID references
        text_classes: CSS classes for the text container
    """
    if not text:
        return

    # Pattern matches "PMID" followed by optional colon/space, then comma-separated numbers.
    # Each number must be 5-8 digits to avoid matching years (e.g. 2025) as PMIDs.
    pattern = re.compile(r"(PMID[:\s]+)(\d{5,8}(?:\s*,\s*\d{5,8})*)", re.IGNORECASE)

    # Build HTML with text segments and badges
    result_parts = []
    last_end = 0

    for match in pattern.finditer(text):
        # Add text before this match (escaped)
        if match.start() > last_end:
            result_parts.append(html.escape(text[last_end : match.start()]))

        # Extract PMIDs (skip the prefix like "PMID: " since badges are self-explanatory)
        pmid_list = match.group(2)

        # Split and create badges for each PMID
        pmids = [p.strip() for p in pmid_list.split(",")]
        pmid_badges = [_get_pubmed_badge_html(pmid) for pmid in pmids]
        result_parts.append(" ".join(pmid_badges))

        last_end = match.end()

    # Add remaining text after last match (escaped)
    if last_end < len(text):
        result_parts.append(html.escape(text[last_end:]))

    # If no PMIDs found, render as justified paragraph
    if not result_parts:
        ui.html(
            f'<p class="{text_classes}" style="text-align:justify;hyphens:auto;'
            f'text-justify:inter-word;margin:0;">{html.escape(text)}</p>'
        )
        return

    # Inject CSS/JS for badges into page head
    _inject_pubmed_badge_styles()

    # Render as HTML to support inline badges with justified text
    html_content = "".join(result_parts)
    ui.html(
        f'<p class="{text_classes}" style="text-align:justify;hyphens:auto;'
        f'text-justify:inter-word;margin:0;">{html_content}</p>'
    )


def transform_pmid_references(text: str) -> str:
    """
    Transform PMID references in text to clickable badge HTML.

    Useful for post-processing markdown content before rendering.
    Does NOT HTML-escape the surrounding text (assumes it will be
    rendered as markdown/HTML).

    Patterns matched:
    - "PMID: 12345678"
    - "PMID 12345678"
    - "PMID: 12345678, 87654321"
    - Markdown links like [PMID: 12345678](url)

    Args:
        text: The text containing PMID references

    Returns:
        Text with PMID references replaced by badge HTML
    """
    if not text:
        return text

    # Pattern matches "PMID" followed by optional colon/space, then comma-separated numbers.
    # Each number must be 5-8 digits to avoid matching years (e.g. 2025) as PMIDs.
    # But NOT when inside a markdown link [text](url)
    pattern = re.compile(r"(?<!\[)(PMID[:\s]+)(\d{5,8}(?:\s*,\s*\d{5,8})*)", re.IGNORECASE)

    def replace_pmid(match: re.Match[str]) -> str:
        pmid_list = match.group(2)
        pmids = [p.strip() for p in pmid_list.split(",")]
        badges = [_get_pubmed_badge_html(pmid) for pmid in pmids]
        return " ".join(badges)

    return pattern.sub(replace_pmid, text)


def render_justified_text(
    text: str,
    text_classes: str = "text-sm text-gray-700",
) -> None:
    """
    Render text as a justified paragraph for better readability.

    Uses text-align: justify with automatic hyphenation for clean
    paragraph formatting in large text blocks.

    Args:
        text: The text to render
        text_classes: CSS classes for styling (color, size, etc.)
    """
    if not text:
        return

    ui.html(
        f'<p class="{text_classes}" style="text-align:justify;hyphens:auto;'
        f'text-justify:inter-word;margin:0;">{html.escape(text)}</p>'
    )


def render_error_card(error_info: dict[str, Any], job_info: JobInfo, job_dir: Path) -> None:
    """
    Render a user-friendly error card with tiered disclosure.

    Creates a visually prominent error display with:
    - User-friendly title and message at the top
    - Actionable steps in bullet list
    - Collapsible sections for additional context and technical details

    Args:
        error_info: Dict from get_user_friendly_error() with keys:
                   category, title, message, extracted_error, steps, raw, contact_admin
        job_info: JobInfo object for the failed job
        job_dir: Path to job directory (for potential log access)
    """
    with ui.card().classes("w-full bg-red-50 border-2 border-red-300 mb-4 p-6"):
        # Header with icon and title
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("error", size="lg").classes("text-red-600")
            ui.label(error_info["title"]).classes("text-h6 font-bold text-red-800")

        # User-friendly message
        ui.label(error_info["message"]).classes("text-red-700 mb-4")

        # Actionable steps
        if error_info.get("steps"):
            ui.label("How to resolve:").classes("font-bold text-red-800 mb-2")
            with ui.column().classes("gap-2 mb-4 pl-2"):
                for step in error_info["steps"]:
                    with ui.row().classes("gap-2 items-start"):
                        ui.label("•").classes("text-red-600 font-bold")
                        ui.label(step).classes("text-sm text-red-700")

        # Contact admin button if needed
        if error_info.get("contact_admin"):
            ui.label("This error requires administrator assistance.").classes(
                "text-sm text-red-600 italic mt-2 mb-3 px-2 py-2 bg-red-100 rounded"
            )

        # Collapsible: What happened
        with (
            ui.expansion("What happened?", icon="info").classes(
                "w-full mt-3 border border-red-200 rounded"
            ),
            ui.column().classes("gap-2 p-3"),
        ):
            ui.label(f"Error Category: {error_info['category'].title()}").classes(
                "text-sm font-bold text-gray-800"
            )
            ui.label(f"Job Status: {job_info.status.value}").classes("text-sm text-gray-700")
            ui.label(
                f"Iterations Completed: {job_info.iterations_completed}/{job_info.max_iterations}"
            ).classes("text-sm text-gray-700")
            if job_info.failed_at:
                ui.label(f"Failed At: {job_info.failed_at[:19]}").classes("text-sm text-gray-700")

            # Extracted error message
            if error_info.get("extracted_error"):
                ui.label("Error Message:").classes("text-sm font-bold mt-3 text-gray-800")
                with ui.element("div").classes("w-full overflow-hidden"):
                    ui.label(error_info["extracted_error"]).classes(
                        "text-sm bg-white p-3 rounded border border-red-200 text-gray-700"
                    ).style("word-break: break-word; overflow-wrap: break-word;")

        # Collapsible: Technical details
        with (
            ui.expansion("Technical Details", icon="code").classes(
                "w-full mt-2 border border-red-200 rounded overflow-hidden"
            ),
            ui.column().classes("gap-2 p-3 w-full"),
        ):
            with ui.row().classes("items-center justify-between w-full mb-2 flex-nowrap"):
                ui.label("Raw Error Output:").classes("text-sm font-bold text-gray-800")

                def copy_to_clipboard() -> None:
                    ui.run_javascript(
                        f"""
                            navigator.clipboard.writeText({error_info["raw"]!r});
                        """
                    )
                    ui.notify("Error message copied to clipboard", type="positive")

                with (
                    ui.button(icon="content_copy", on_click=copy_to_clipboard)
                    .props("flat dense color=primary size=sm")
                    .classes("flex-shrink-0")
                ):
                    ui.tooltip("Copy to clipboard")

            # Wrap code block in container to prevent overflow
            with ui.element("div").classes("w-full overflow-x-auto"):
                ui.code(error_info["raw"], language="text").classes(
                    "text-xs max-h-[300px] overflow-y-auto p-2 bg-gray-50 rounded"
                ).style("word-break: break-word; white-space: pre-wrap; max-width: 100%;")

            # Link to logs if available
            log_file = job_dir / "orchestrator.log"
            if log_file.exists():
                ui.label("Check the orchestrator.log file for complete details.").classes(
                    "text-xs text-gray-600 mt-2"
                )


def render_config_error_banner(
    provider_name: str,
    config_errors: list[str],
    show_back_button: bool = False,
) -> None:
    """
    Render a configuration error banner with consistent styling.

    This is a reusable component for displaying provider configuration errors
    across the application. Use this instead of creating custom error displays.

    Args:
        provider_name: Name of the misconfigured provider (e.g., "anthropic")
        config_errors: List of specific error messages
        show_back_button: Whether to show a "Back to Jobs" button
    """
    # Use a wrapper div with padding to ensure proper spacing on mobile
    # This avoids the w-full + margin overflow issue
    with (
        ui.element("div").classes("w-full px-4 mt-4 box-border"),
        ui.card().classes("w-full bg-red-50 border-l-4 border-red-500"),
    ):
        with ui.row().classes("items-start gap-3 flex-wrap"):
            ui.icon("error", color="red", size="md").classes("flex-shrink-0 mt-1")
            with ui.column().classes("gap-1 flex-1 min-w-0"):
                ui.label("Server Configuration Error").classes(
                    "text-red-800 font-bold text-base sm:text-lg"
                )
                ui.label(
                    f"The {provider_name.upper()} provider is not configured correctly. "
                    "Jobs cannot be started until this is resolved."
                ).classes("text-red-700 text-sm sm:text-base break-words")
                ui.label("Please contact the system administrator.").classes(
                    "text-red-600 text-xs sm:text-sm"
                )

        with ui.expansion("Technical Details", icon="info").classes("mt-2 w-full"):
            for error in config_errors:
                ui.label(f"• {error}").classes(
                    "text-red-600 text-xs sm:text-sm font-mono break-words"
                )

        if show_back_button:
            ui.button(
                "Back to Jobs",
                on_click=lambda: ui.navigate.to("/jobs"),
                icon="arrow_back",
            ).classes("mt-4")


def render_alert_banner(
    title: str,
    message: str,
    severity: str = "error",
    details: list[str] | None = None,
    expansion_title: str = "Details",
) -> None:
    """
    Render a generic alert banner with consistent styling.

    This is a reusable component for displaying alerts (errors, warnings, info)
    across the application.

    Args:
        title: Alert title
        message: Main alert message
        severity: One of "error", "warning", "info" (affects colors)
        details: Optional list of detail messages shown in expansion
        expansion_title: Title for the expandable details section
    """
    # Color mappings
    colors = {
        "error": {
            "bg": "bg-red-50",
            "border": "border-red-500",
            "icon_color": "red",
            "title": "text-red-800",
            "message": "text-red-700",
            "detail": "text-red-600",
        },
        "warning": {
            "bg": "bg-yellow-50",
            "border": "border-yellow-500",
            "icon_color": "orange",
            "title": "text-yellow-800",
            "message": "text-yellow-700",
            "detail": "text-yellow-600",
        },
        "info": {
            "bg": "bg-blue-50",
            "border": "border-blue-500",
            "icon_color": "blue",
            "title": "text-blue-800",
            "message": "text-blue-700",
            "detail": "text-blue-600",
        },
    }
    c = colors.get(severity, colors["error"])
    icon_name = {"error": "error", "warning": "warning", "info": "info"}.get(severity, "error")

    # Use a wrapper div with padding to ensure proper spacing on mobile
    with (
        ui.element("div").classes("w-full px-4 mt-4 box-border"),
        ui.card().classes(f"w-full {c['bg']} border-l-4 {c['border']}"),
    ):
        with ui.row().classes("items-start gap-3 flex-wrap"):
            ui.icon(icon_name, color=c["icon_color"], size="md").classes("flex-shrink-0 mt-1")
            with ui.column().classes("gap-1 flex-1 min-w-0"):
                ui.label(title).classes(f"{c['title']} font-bold text-base sm:text-lg")
                ui.label(message).classes(f"{c['message']} text-sm sm:text-base break-words")

        if details:
            with ui.expansion(expansion_title, icon="info").classes("mt-2 w-full"):
                for detail in details:
                    ui.label(f"• {detail}").classes(
                        f"{c['detail']} text-xs sm:text-sm font-mono break-words"
                    )


def get_status_badge_props(status: JobStatus) -> dict[str, Any]:
    """
    Get NiceGUI badge properties for a job status.

    Args:
        status: JobStatus enum value

    Returns:
        Dict with keys: color, icon, text, classes
    """
    color = STATUS_COLORS.get(status, "gray")
    icon = STATUS_ICONS.get(status, "○")

    # Special styling for failed jobs
    classes = ""
    if status == JobStatus.FAILED:
        classes = "bg-red-600 text-white font-bold"

    return {
        "color": color,
        "icon": icon,
        "text": status.value.replace("_", " "),
        "classes": classes,
    }


def render_status_cell_slot() -> str:
    """
    Generate Quasar table slot template for status column with enhanced failed job display.

    Returns slot template string with:
    - Colored badges based on status
    - Icons for each status type
    - Red background with white text for failed jobs
    - Tooltip showing error preview on hover for failed jobs

    Returns:
        Quasar slot template string
    """
    return r"""
        <q-td :props="props">
            <div class="row items-center gap-2">
                <!-- Failed status: Red badge with white text and error tooltip -->
                <q-badge
                    v-if="props.row.status === 'failed'"
                    color="red"
                    text-color="white"
                    class="px-3 py-1 font-bold cursor-pointer"
                >
                    <span class="row items-center" style="white-space:nowrap;">✗&nbsp;{{ props.row.status }}</span>
                    <q-tooltip
                        v-if="props.row.error"
                        class="bg-red-800 text-white text-sm"
                        max-width="400px"
                        anchor="top middle"
                        self="bottom middle"
                    >
                        <div class="font-bold mb-1">Error:</div>
                        <div>{{ props.row.error.substring(0, 150) }}{{ props.row.error.length > 150 ? '...' : '' }}</div>
                        <div class="text-xs mt-2 italic">Click "View" for details</div>
                    </q-tooltip>
                </q-badge>

                <!-- Completed status: Green badge -->
                <q-badge
                    v-else-if="props.row.status === 'completed'"
                    color="green"
                    class="px-2 py-1"
                >
                    <span class="row items-center" style="white-space:nowrap;">✓&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Running status: Teal badge -->
                <q-badge
                    v-else-if="props.row.status === 'running'"
                    color="teal"
                    class="px-2 py-1"
                >
                    <span class="row items-center" style="white-space:nowrap;">▶&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Queued status: Blue badge -->
                <q-badge
                    v-else-if="props.row.status === 'queued'"
                    color="blue"
                    class="px-2 py-1"
                >
                    <span class="row items-center" style="white-space:nowrap;">⟳&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Awaiting feedback: Orange badge -->
                <q-badge
                    v-else-if="props.row.status === 'awaiting_feedback'"
                    color="orange"
                    class="px-2 py-1"
                >
                    <span class="row items-center" style="white-space:nowrap;">⏸&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Generating report status: Teal badge -->
                <q-badge
                    v-else-if="props.row.status === 'generating_report'"
                    color="teal"
                    class="px-2 py-1"
                >
                    <span class="row items-center" style="white-space:nowrap;">⟳&nbsp;generating report</span>
                </q-badge>

                <!-- Cancelled status: Gray badge -->
                <q-badge
                    v-else-if="props.row.status === 'cancelled'"
                    color="grey"
                    class="px-2 py-1"
                >
                    <span class="row items-center" style="white-space:nowrap;">⊗&nbsp;{{ props.row.status }}</span>
                </q-badge>

                <!-- Default: Gray badge -->
                <q-badge
                    v-else
                    color="grey"
                    class="px-2 py-1"
                >
                    <span class="row items-center" style="white-space:nowrap;">○&nbsp;{{ props.row.status }}</span>
                </q-badge>
            </div>
        </q-td>
    """


def render_actions_slot_with_delete() -> str:
    """
    Generate Quasar table slot template for actions column with share and delete.

    Returns slot template string with:
    - Share icon button (conditionally shown via v-if="props.row.can_share") - uses share icon
    - Delete icon button (conditionally shown via v-if="props.row.can_delete") - uses delete icon
    - All buttons use round style for a compact, badge-like appearance
    - Tooltips for clarity

    Note: View functionality is handled by clicking the job ID badge.
    Note: Notifications are configured on the job detail page.

    Returns:
        Quasar slot template string
    """
    return r"""
        <q-td :props="props">
            <div class="row items-center gap-1 justify-center">
                <!-- Share button - conditionally shown based on can_share (owners only) -->
                <q-btn
                    v-if="props.row.can_share"
                    round
                    flat
                    dense
                    size="sm"
                    color="primary"
                    icon="share"
                    @click="$parent.$emit('share-job', props.row.job_id)"
                >
                    <q-tooltip>Share job</q-tooltip>
                </q-btn>

                <!-- Delete button - conditionally shown based on can_delete -->
                <q-btn
                    v-if="props.row.can_delete"
                    round
                    flat
                    dense
                    size="sm"
                    color="negative"
                    icon="delete"
                    @click="$parent.$emit('delete-job', props.row.job_id)"
                >
                    <q-tooltip>Delete job</q-tooltip>
                </q-btn>
            </div>
        </q-td>
    """


def render_job_action_buttons(
    on_share: Callable[[], None] | None = None,
    on_delete: Callable[[], None] | None = None,
    on_notifications: Callable[[], None] | None = None,
) -> None:
    """
    Render job action buttons (share, delete, notifications) in the same style as table actions.

    Uses round, flat, dense icon buttons with tooltips - same visual style as
    the table action column buttons from render_actions_slot_with_delete().

    Args:
        on_share: Callback for share button click. If None, share button is hidden.
        on_delete: Callback for delete button click. If None, delete button is hidden.
        on_notifications: Callback for notifications button click. If None, button is hidden.
    """
    with ui.row().classes("gap-1 items-center"):
        if on_notifications:
            with ui.button(icon="notifications", on_click=on_notifications).props(
                "round flat dense size=sm color=primary"
            ):
                ui.tooltip("Configure push notifications")

        if on_share:
            with ui.button(icon="share", on_click=on_share).props(
                "round flat dense size=sm color=primary"
            ):
                ui.tooltip("Share job")

        if on_delete:
            with ui.button(icon="delete", on_click=on_delete).props(
                "round flat dense size=sm color=negative"
            ):
                ui.tooltip("Delete job")


def _inject_navigation_responsive_css() -> None:
    """Inject responsive CSS toggling mobile drawer button vs desktop nav."""
    ui.add_css(
        """
        @media (max-width: 1023px) {
            .mobile-menu-btn { display: inline-flex !important; }
            .desktop-nav { display: none !important; }
        }
        @media (min-width: 1024px) {
            .mobile-menu-btn { display: none !important; }
            .desktop-nav { display: flex !important; }
        }
    """
    )


def _build_navigation_items(
    active_page: str | None,
    show_new_job: bool,
    can_start_jobs: bool,
    show_admin: bool,
) -> list[tuple[str, str, str, bool]]:
    """Build ordered navigation entries for both desktop and mobile UIs."""
    nav_items: list[tuple[str, str, str, bool]] = []
    if show_new_job and can_start_jobs:
        nav_items.append(("New", "add", "/new", active_page == "new"))
    nav_items.extend(
        [
            ("Skills", "school", "/skills", active_page == "skills"),
            ("API Keys", "vpn_key", "/api-keys", active_page == "api-keys"),
            ("Docs", "description", "/docs", active_page == "docs"),
        ]
    )
    if show_admin:
        nav_items.append(("Admin", "admin_panel_settings", "/admin", active_page == "admin"))
    return nav_items


def _drawer_click_handler(drawer: ui.element, callback: Callable[[], None]) -> Callable[[], None]:
    """Wrap drawer click callbacks to close the drawer first."""

    def handler() -> None:
        cast(Any, drawer).set_value(False)
        callback()

    return handler


def _drawer_route_handler(drawer: ui.element, route: str) -> Callable[[], None]:
    """Create navigation handler for drawer route buttons."""

    def handler() -> None:
        cast(Any, drawer).set_value(False)
        ui.navigate.to(route)

    return handler


def _render_mobile_drawer(
    drawer: ui.element,
    nav_items: list[tuple[str, str, str, bool]],
    extra_buttons: list[tuple[str, str, Callable[[], None], str]] | None,
    active_style: str,
) -> None:
    """Render right-side mobile drawer navigation."""
    drawer.classes("bg-primary")
    with ui.column().classes("w-full gap-2 p-4"):
        ui.label("Navigation").classes("text-white text-h6 font-bold mb-2")
        for label, icon, on_click, _props in extra_buttons or []:
            ui.button(
                label,
                on_click=_drawer_click_handler(drawer, on_click),
                icon=icon,
            ).props("flat color=white align=left").classes("w-full justify-start")

        for label, icon, route, is_active in nav_items:
            style = active_style if is_active else "flat color=white"
            ui.button(
                label,
                on_click=_drawer_route_handler(drawer, route),
                icon=icon,
            ).props(f"{style} align=left").classes("w-full justify-start")

        ui.separator().classes("bg-white/30 my-2")
        ui.button(
            "Logout",
            on_click=lambda: ui.navigate.to("/auth/logout"),
            icon="logout",
        ).props("flat color=white align=left").classes("w-full justify-start")


def _render_navigation_brand() -> None:
    """Render OpenScientist brand/link in header left section."""
    with (
        ui.link(target="/jobs").classes("no-underline"),
        ui.row().classes("items-center gap-2 cursor-pointer"),
    ):
        with ui.element("div").classes(
            "w-10 h-10 rounded-full bg-white flex items-center justify-center"
        ):
            ui.image("/assets/logo.svg").classes("w-8 h-8").style(
                "width:32px;height:32px;min-width:32px;min-height:32px;"
            )
        ui.label("OpenScientist").classes("text-white text-h5 font-bold")


def _render_desktop_navigation(
    nav_items: list[tuple[str, str, str, bool]],
    extra_buttons: list[tuple[str, str, Callable[[], None], str]] | None,
    active_style: str,
    inactive_style: str,
) -> None:
    """Render desktop navigation button row."""
    nav_row = ui.row().classes("gap-1 desktop-nav")
    nav_row.style("display: flex")
    with nav_row:
        for label, icon, on_click, props in extra_buttons or []:
            btn = ui.button(label, on_click=on_click, icon=icon)
            if props:
                btn.props(props)
        for label, icon, route, is_active in nav_items:
            style = active_style if is_active else inactive_style
            ui.button(
                label,
                on_click=lambda r=route: ui.navigate.to(r),
                icon=icon,
            ).props(style)
        ui.button(
            "Logout",
            on_click=lambda: ui.navigate.to("/auth/logout"),
            icon="logout",
        ).props(inactive_style)


def render_navigator(
    active_page: str | None = None,
    show_new_job: bool = True,
    extra_buttons: list[tuple[str, str, Callable[[], None], str]] | None = None,
) -> None:
    """
    Render the standard navigation header for all authenticated pages.

    Provides consistent navigation across the application with links to
    New Job, Billing, Docs, and Admin pages. The OpenScientist logo/title acts
    as a home button linking to the jobs list.
    """
    _inject_navigation_responsive_css()

    show_admin = app.storage.user.get("is_admin", False)
    can_start_jobs = app.storage.user.get("can_start_jobs", False)
    active_style = "unelevated color=white text-color=primary"
    inactive_style = "flat color=white"
    nav_items = _build_navigation_items(active_page, show_new_job, can_start_jobs, show_admin)

    with ui.right_drawer(value=False).props("overlay behavior=mobile bordered") as drawer:
        _render_mobile_drawer(drawer, nav_items, extra_buttons, active_style)

    with ui.header().classes("items-center justify-between"):
        _render_navigation_brand()
        hamburger = ui.button(icon="menu", on_click=lambda: drawer.set_value(True)).props(
            "flat color=white"
        )
        hamburger.style("display: none").classes("mobile-menu-btn")
        _render_desktop_navigation(nav_items, extra_buttons, active_style, inactive_style)


def render_pending_approval_notice() -> None:
    """Render an informational notice for users awaiting administrator approval."""
    with (
        ui.card().classes("w-full border-l-4 border-amber-500 bg-amber-50"),
        ui.row().classes("items-start gap-3"),
    ):
        ui.icon("hourglass_top", color="amber", size="md")
        with ui.column().classes("gap-1"):
            ui.label("Account Pending Approval").classes("text-amber-900 font-bold")
            ui.label(
                "Your account is waiting for administrator approval. "
                "You can browse existing pages, but starting new jobs "
                "is disabled until approval."
            ).classes("text-amber-800")


def render_stat_badges(
    stats: list[tuple[str, str | int, str]],
    icon_map: dict[str, str] | None = None,
) -> None:
    """
    Render a compact row of stat badges - mobile-friendly inline display.

    Creates a responsive row of badges with icons, labels, and values.
    Wraps gracefully on mobile screens.

    Args:
        stats: List of (label, value, color) tuples where color is a Quasar color name
               (e.g., "blue", "green", "red") or empty string for default gray
        icon_map: Optional mapping of label to Material icon name

    Example:
        render_stat_badges([
            ("Total", 42, ""),
            ("Running", 3, "blue"),
            ("Completed", 39, "green"),
        ])
    """
    default_icons = {
        "Total": "list",
        "Total Jobs": "list",
        "Running": "play_circle",
        "Completed": "check_circle",
        "Failed": "error",
        "Status": "info",
        "Progress": "trending_up",
        "Findings": "lightbulb",
        "Papers": "article",
        "Papers Reviewed": "article",
        "Skills": "school",
        "Model": "psychology",
    }
    icons = {**default_icons, **(icon_map or {})}

    with ui.row().classes("w-full gap-2 flex-wrap items-center mb-2"):
        for label, value, color in stats:
            badge_color = color if color else "gray"
            icon = icons.get(label, "tag")
            with (
                ui.badge(color=badge_color).props("outline").classes("px-3 py-1 text-sm"),
                ui.row().classes("items-center gap-1"),
            ):
                ui.icon(icon, size="xs")
                ui.label(f"{label}: {value}").classes("font-medium")


def render_empty_state(message: str) -> None:
    """
    Render a styled empty state message.

    Args:
        message: The message to display
    """
    ui.label(message).classes("text-gray-500 text-center p-8")


def render_permission_badge_slot() -> str:
    """
    Generate Quasar table slot template for permission level column.

    Returns slot template string that renders permission level as a colored badge
    (orange for 'edit', blue for 'view').

    Returns:
        Quasar slot template string
    """
    return r"""
        <q-td :props="props">
            <q-badge :color="props.row.permission === 'edit' ? 'orange' : 'blue'">
                {{ props.row.permission }}
            </q-badge>
        </q-td>
    """


def render_not_found_state(
    title: str,
    message: str,
    back_label: str = "Go Back",
    back_url: str | None = None,
    icon: str = "search_off",
) -> None:
    """
    Render a styled 404/not found state.

    Creates a centered column with an icon, title, message, and optional back button.

    Args:
        title: The main heading (e.g., "Skill not found")
        message: Detailed message explaining what wasn't found
        back_label: Text for the back button
        back_url: URL to navigate to when back button is clicked.
                  If None, no back button is shown.
        icon: Material icon name to display (default: "search_off")
    """
    with ui.column().classes("w-full items-center py-16"):
        ui.icon(icon, size="xl").classes("text-gray-300 mb-4")
        ui.label(title).classes("text-h5 font-bold text-gray-600 mb-2")
        ui.label(message).classes("text-gray-500 mb-4")
        if back_url:
            ui.button(
                back_label,
                on_click=lambda: ui.navigate.to(back_url),
                icon="arrow_back",
            ).props("color=primary")


def render_error_state(
    title: str,
    message: str,
    back_label: str = "Go Back",
    back_url: str | None = None,
    icon: str = "error",
) -> None:
    """
    Render a styled error state.

    Creates a centered column with an error icon, title, message, and optional back button.

    Args:
        title: The main heading (e.g., "Failed to load skill")
        message: Detailed error message
        back_label: Text for the back button
        back_url: URL to navigate to when back button is clicked.
                  If None, no back button is shown.
        icon: Material icon name to display (default: "error")
    """
    with ui.column().classes("w-full items-center py-16"):
        ui.icon(icon, size="xl").classes("text-red-300 mb-4")
        ui.label(title).classes("text-h5 font-bold text-red-600 mb-2")
        ui.label(message).classes("text-gray-500 mb-4")
        if back_url:
            ui.button(
                back_label,
                on_click=lambda: ui.navigate.to(back_url),
                icon="arrow_back",
            ).props("color=primary")


def render_loading_spinner(message: str = "Loading...") -> ui.element:
    """
    Render a centered loading spinner with message.

    Args:
        message: Text to display next to spinner

    Returns:
        The row container element (can be used to show/hide)
    """
    with ui.row().classes("w-full justify-center py-16") as container:
        ui.spinner(size="lg")
        ui.label(message).classes("ml-4 text-gray-500")
    return container


def render_dialog_actions(
    on_confirm: Callable[[], None | Awaitable[None]],
    on_cancel: Callable[[], None],
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
    confirm_props: str = "color=primary",
) -> None:
    """
    Render standard dialog action buttons (Cancel/Confirm).

    Creates a right-aligned row with Cancel and Confirm buttons.
    Use this in dialogs to ensure consistent footer styling.

    Args:
        on_confirm: Callback when confirm button is clicked (can be async)
        on_cancel: Callback when cancel button is clicked
        confirm_label: Text for the confirm button
        cancel_label: Text for the cancel button
        confirm_props: Quasar props for the confirm button
    """
    with ui.row().classes("w-full justify-end gap-2 mt-4"):
        ui.button(cancel_label, on_click=on_cancel).props("flat")
        ui.button(confirm_label, on_click=on_confirm).props(confirm_props)


def make_action_button_slot(
    label: str,
    event_name: str,
    icon: str | None = None,
    color: str = "primary",
) -> str:
    """
    Generate a Quasar table slot template for an action button.

    Creates an HTML template string for use with NiceGUI's table.add_slot().
    The button emits an event with the row data when clicked.

    Args:
        label: Button label text
        event_name: Event name emitted when button is clicked
        icon: Optional Material icon name (e.g., "person_add")
        color: Quasar color for the button

    Returns:
        Quasar slot template string

    Example:
        table.add_slot("body-cell-actions", make_action_button_slot(
            label="Assign",
            event_name="assign",
            icon="person_add",
        ))
        table.on("assign", handle_assign)
    """
    icon_attr = f'icon="{icon}"' if icon else ""
    return f"""
<q-td :props="props">
    <q-btn
        size="sm"
        color="{color}"
        {icon_attr}
        label="{label}"
        @click="$parent.$emit('{event_name}', props.row)"
    />
</q-td>
"""


async def render_user_search(
    on_select: AsyncCallback,
    placeholder: str = "Search by email or name",
    action_label: str = "Select",
    action_icon: str = "check",
) -> tuple[ui.input, ui.column]:
    """
    Render a user search input with results list.

    Creates a search input that queries users by email/name and displays
    results with action buttons. Used for share dialogs and admin assignment.

    Args:
        on_select: Async callback when a user is selected, receives user dict
                   with keys: id, name, email
        placeholder: Placeholder text for the search input
        action_label: Label for the select button
        action_icon: Icon for the select button

    Returns:
        Tuple of (search_input, results_container) for external reference
    """
    search_input = ui.input(
        label=placeholder,
        placeholder="Type to search...",
    ).classes("w-full mb-4")

    results_container = ui.column().classes("w-full max-h-48 overflow-y-auto")

    async def search_users() -> None:
        """Search for users by email or name."""
        query = search_input.value
        if not query or len(query) < 2:
            results_container.clear()
            return

        try:
            # Use admin session to search all users
            async with get_admin_session() as session:
                stmt = (
                    select(User)
                    .where(User.email.ilike(f"%{query}%") | User.name.ilike(f"%{query}%"))
                    .limit(10)
                )
                result = await session.execute(stmt)
                users = result.scalars().all()

            results_container.clear()
            with results_container:
                if not users:
                    ui.label("No users found").classes("text-gray-500 text-sm p-2")
                else:
                    for user in users:
                        with ui.row().classes(
                            "w-full items-center gap-2 p-2 hover:bg-gray-100 cursor-pointer"
                        ):
                            ui.label(f"{user.name} ({user.email})").classes("flex-grow text-sm")

                            async def select_user(u: Any = user) -> None:
                                user_data = {
                                    "id": u.id,
                                    "name": u.name,
                                    "email": u.email,
                                }
                                await on_select(user_data)

                            ui.button(
                                action_label,
                                icon=action_icon,
                                on_click=select_user,
                            ).props("size=sm flat")

        except Exception as e:
            logger.error("Failed to search users: %s", e, exc_info=True)
            results_container.clear()
            with results_container:
                ui.label("Search failed").classes("text-red-500 text-sm")

    async def _on_search_change(_e: Any) -> None:
        await search_users()

    search_input.on_value_change(_on_search_change)

    return search_input, results_container


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


async def _search_share_targets(search_query: str) -> list[User]:
    """Search active users by email/name for share target picker."""
    search_pattern = f"%{search_query}%"
    async with get_admin_session() as session:
        result = await session.execute(
            select(User)
            .where(or_(User.email.ilike(search_pattern), User.name.ilike(search_pattern)))
            .where(User.is_active.is_(True))
            .order_by(User.email)
            .limit(10)
        )
    return list(result.scalars().all())


def _render_share_search_results(
    search_results: ui.element,
    users: list[User],
    on_select: Callable[[str, str], None],
) -> None:
    """Render selectable cards for searched users."""
    search_results.clear()
    with search_results:
        if not users:
            ui.label("No users found").classes("text-gray-500 italic")
            return
        for user in users:
            with (
                ui.card()
                .classes("w-full p-2 cursor-pointer hover:bg-blue-50")
                .on("click", lambda u=user: on_select(u.email, u.name or u.email)),
                ui.row().classes("items-center gap-2"),
            ):
                ui.icon("person_outline", size="sm").classes("text-gray-400")
                with ui.column().classes("gap-0"):
                    ui.label(user.name or user.email).classes("font-bold text-sm")
                    if user.name:
                        ui.label(user.email).classes("text-xs text-gray-600")


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
        search_input: ui.input,
        search_results: ui.element,
        selected_user_container: ui.element,
        share_action_row: ui.element,
        permission_select: ui.select,
    ) -> None:
        self.job_id = job_id
        self.shares_container = shares_container
        self.search_input = search_input
        self.search_results = search_results
        self.selected_user_container = selected_user_container
        self.share_action_row = share_action_row
        self.permission_select = permission_select
        self.selected_user_state: dict[str, str | None] = {"email": None, "name": None}
        self.search_counter = {"value": 0}

    def clear_selection(self) -> None:
        """Clear selected share target and show search input again."""
        self.selected_user_state["email"] = None
        self.selected_user_state["name"] = None
        self.selected_user_container.clear()
        self.share_action_row.classes(add="hidden")
        self.search_input.visible = True
        self.search_input.value = ""

    def select_user(self, email: str, name: str) -> None:
        """Select user from search results and reveal share controls."""
        self.selected_user_state["email"] = email
        self.selected_user_state["name"] = name
        self.search_results.clear()
        self.search_input.visible = False
        _render_selected_share_user(self.selected_user_container, email, name, self.clear_selection)
        self.share_action_row.classes(remove="hidden")

    async def refresh_shares(self) -> None:
        """Reload current shares list from database."""
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

    async def search_users(self, search_query: str) -> None:
        """Search users and render result cards with debounce guard."""
        self.search_counter["value"] += 1
        search_index = self.search_counter["value"]
        self.search_results.clear()
        if not search_query or len(search_query) < 2:
            return
        try:
            users = await _search_share_targets(search_query)
            if search_index == self.search_counter["value"]:
                _render_share_search_results(self.search_results, users, self.select_user)
        except Exception as exc:
            logger.error("Failed to search users: %s", exc, exc_info=True)
            with self.search_results:
                ui.label("Search failed").classes("text-red-600")

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

    async def on_search_change(self, event: Any) -> None:
        """Handle search-input model updates."""
        await self.search_users(event.value)


def render_share_dialog(job_id: str) -> ui.dialog:
    """Create and return a share dialog for a job."""
    with ui.dialog() as dialog, ui.card().classes("w-[600px]"):
        with ui.row().classes("items-center gap-2 mb-4"):
            ui.label("Share Job").classes("text-h6")
            render_job_id_badge(job_id)

        shares_container = ui.column().classes("w-full mb-4")
        ui.separator()
        ui.label("Add New Share").classes("text-subtitle2 font-bold mb-2")
        search_input = ui.input("Search by email or name", placeholder="user@example.com").classes(
            "w-full"
        )
        search_results = ui.column().classes("w-full")
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
            search_input=search_input,
            search_results=search_results,
            selected_user_container=selected_user_container,
            share_action_row=share_action_row,
            permission_select=permission_select,
        )
        with share_action_row:
            ui.button("Share", icon="person_add", on_click=controller.do_share).props(
                "color=primary"
            )

        search_input.on_value_change(controller.on_search_change)

        with ui.row().classes("w-full justify-end gap-2 mt-4"):
            ui.button("Close", on_click=dialog.close)
        dialog.on("open", controller.refresh_shares)

    return dialog


async def _fetch_notification_settings(user_id: str) -> tuple[bool, str | None] | None:
    """Fetch current ntfy settings for user."""
    async with get_session_ctx() as session:
        await set_current_user(session, UUID(user_id))
        result = await session.execute(
            select(User.ntfy_enabled, User.ntfy_topic).where(User.id == UUID(user_id))
        )
        row = result.first()
    if row is None:
        return None
    return row.ntfy_enabled, row.ntfy_topic


async def _set_notifications_enabled(user_id: str, enabled: bool) -> None:
    """Persist ntfy_enabled flag."""
    async with get_session_ctx() as session:
        await set_current_user(session, UUID(user_id))
        stmt = update(User).where(User.id == UUID(user_id)).values(ntfy_enabled=enabled)
        await session.execute(stmt)
        await session.commit()


def _render_ntfy_topic_details(ntfy_topic: str) -> None:
    """Render subscription links and copy controls for ntfy topic."""
    subscription_url = get_subscription_url(ntfy_topic)
    ui.separator().classes("my-2")
    ui.label("Subscribe to your notifications").classes("text-subtitle2 font-bold mb-2")

    with ui.column().classes("gap-2"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("computer", size="sm").classes("text-blue-500")
            ui.link("Open in browser", target=subscription_url, new_tab=True).classes(
                "text-blue-600 underline"
            )
        with ui.row().classes("items-center gap-2"):
            ui.icon("phone_android", size="sm").classes("text-green-500")
            ui.markdown(
                f"**Mobile**: Install [ntfy app](https://ntfy.sh/app) and subscribe to `{ntfy_topic}`"
            )

        def copy_topic() -> None:
            ui.run_javascript(f'navigator.clipboard.writeText("{ntfy_topic}")')
            ui.notify("Copied!", type="positive")

        with ui.row().classes("items-center gap-2 mt-2"):
            topic_input = ui.input(value=ntfy_topic, label="Your topic").classes("flex-grow")
            topic_input.props("readonly outlined dense")
            ui.button(icon="content_copy", on_click=copy_topic).props("flat round").tooltip(
                "Copy topic"
            )


def _render_ntfy_test_button(ntfy_topic: str) -> None:
    """Render button to send a test ntfy notification."""
    ui.separator().classes("my-2")

    async def send_test() -> None:
        success = await send_notification(
            topic=ntfy_topic,
            title="OpenScientist Test",
            message="Test notification - if you see this, it works!",
            tags=["white_check_mark"],
        )
        ui.notify(
            "Test sent!" if success else "Failed to send",
            type="positive" if success else "negative",
        )

    ui.button(
        "Send Test Notification",
        on_click=send_test,
        icon="notifications_active",
    ).props("color=primary")


async def _render_notifications_content(
    content_container: ui.element,
    user_id: str,
    reload_content: Callable[[], Awaitable[None]],
) -> None:
    """Fetch and render notifications content inside dialog."""
    content_container.clear()
    try:
        settings = await _fetch_notification_settings(user_id)
        if settings is None:
            with content_container:
                ui.label("User not found").classes("text-red-500")
            return
        ntfy_enabled, ntfy_topic = settings
        if ntfy_enabled and not ntfy_topic:
            ntfy_topic = await ensure_user_has_topic(UUID(user_id))
    except Exception as exc:
        logger.error("Error loading settings: %s", exc, exc_info=True)
        with content_container:
            ui.label("Error loading settings. Check server logs.").classes("text-red-500")
        return

    with content_container:

        async def toggle_notifications(event: Any) -> None:
            new_value = event.value
            try:
                await _set_notifications_enabled(user_id, new_value)
                ui.notify(
                    "Notifications enabled" if new_value else "Notifications disabled",
                    type="positive",
                )
                await reload_content()
            except Exception as exc:
                logger.error("Failed to toggle notifications: %s", exc, exc_info=True)
                ui.notify(f"Failed to update: {exc}", type="negative")

        ui.switch(
            "Enable push notifications",
            value=ntfy_enabled,
            on_change=toggle_notifications,
        ).classes("mb-4")

        if not ntfy_enabled:
            ui.label(
                "Enable notifications to receive alerts when jobs complete, fail, or need feedback."
            ).classes("text-gray-500")
            return
        if not ntfy_topic:
            ui.label("Setting up your notification topic...").classes("text-gray-500")
            return
        _render_ntfy_topic_details(ntfy_topic)
        _render_ntfy_test_button(ntfy_topic)


def render_notifications_dialog(job_id: str, user_id: str | None = None) -> ui.dialog:
    """
    Create and return a notifications dialog for a job.

    Shows the user's ntfy.sh subscription info and allows toggling notifications.
    """
    with ui.dialog() as dialog, ui.card().classes("w-[500px]"):
        with ui.row().classes("items-center gap-2 mb-4"):
            ui.label("Push Notifications").classes("text-h6")
            if job_id and job_id != "notifications" and len(job_id) > 10:
                render_job_id_badge(job_id)

        if not user_id:
            ui.label("Not logged in").classes("text-red-500")
            content_container = None
        else:
            content_container = ui.column().classes("w-full")
            with content_container:
                ui.label("Loading...").classes("text-gray-500")

        with ui.row().classes("w-full justify-end gap-2 mt-4"):
            ui.button("Close", on_click=dialog.close)

        if content_container and user_id:

            async def load_content() -> None:
                await _render_notifications_content(content_container, user_id, load_content)

            dialog.on("show", load_content)

    return dialog


def render_delete_dialog(
    job_id: str,
    job_manager: "JobManager",
    on_deleted: Callable[[], None | Awaitable[None]] | None = None,
) -> ui.dialog:
    """
    Create and return a delete confirmation dialog for a job.

    This is a reusable component for deleting jobs with confirmation.
    If the job is running or queued, it will be cancelled first before deletion.

    Args:
        job_id: The job ID to delete
        job_manager: The job manager instance
        on_deleted: Optional callback to run after successful deletion

    Returns:
        The dialog element (call .open() to show it)

    Example:
        delete_dialog = render_delete_dialog(job_id, job_manager, on_deleted=refresh_table)
        ui.button("Delete", on_click=delete_dialog.open)
    """
    with ui.dialog() as dialog, ui.card().classes("w-96"):
        content_container = ui.column().classes("w-full")

        @ui.refreshable
        def render_dialog_content() -> None:
            content_container.clear()
            job_info = job_manager.get_job(job_id)
            is_active = job_info and job_info.status in [
                JobStatus.RUNNING,
                JobStatus.QUEUED,
            ]

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

        render_dialog_content()

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

                # Check if job needs to be cancelled first
                job_info = job_manager.get_job(job_id)
                if job_info and job_info.status in [
                    JobStatus.RUNNING,
                    JobStatus.QUEUED,
                ]:
                    job_manager.cancel_job(job_id)

                job_manager.delete_job(job_id)
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

        # Refresh content when dialog opens to show current job status
        dialog.on_value_change(lambda e: render_dialog_content.refresh() if e.value else None)

    return dialog


def _inject_thinking_status_styles() -> None:
    """Inject CSS for thinking status indicator into page head (idempotent)."""
    ui.add_head_html(
        """
        <style>
        .thinking-label {
            color: #0891b2;
            font-size: 12px;
            font-weight: 500;
            animation: openscientist-pulse-text 1.5s ease-in-out infinite;
        }
        @keyframes openscientist-pulse-text {
            0%, 100% { opacity: 0.6; }
            50% { opacity: 1; }
        }
        </style>
        """,
        shared=True,
    )


_ASSETS_DIR = Path(__file__).parent.parent / "assets"

# Animated OpenScientist logo SVG — loaded from assets/thinking.svg at import time.
# Rendered inline (via ui.html) so SMIL animations work; <img> tags disable them.
OPENSCIENTIST_THINKING_SVG = (_ASSETS_DIR / "thinking.svg").read_text(encoding="utf-8")


def render_container_status_badge(status: str) -> None:
    """Render a Docker container status as a colored Quasar badge.

    Args:
        status: Docker container status string (running, exited, created, etc.)
    """
    color_map = {
        "running": "green",
        "exited": "grey",
        "created": "blue",
        "restarting": "orange",
        "dead": "red",
        "removing": "red",
        "paused": "grey",
    }
    color = color_map.get(status, "grey")
    ui.badge(status, color=color).classes("px-2 py-1")


def format_uptime(seconds: float) -> str:
    """Format seconds as a human-readable uptime string.

    Examples:
        >>> format_uptime(30)
        '30s'
        >>> format_uptime(90)
        '1m 30s'
        >>> format_uptime(8100)
        '2h 15m'
    """
    if seconds < 0:
        return "0s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    secs = seconds % 60
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def render_thinking_status(status_text: str = "Thinking...") -> ui.element:
    """
    Render an animated OpenScientist thinking/status indicator.

    Creates a visually distinctive status display with:
    - Animated OpenScientist logo with orbiting circles
    - Status text describing what the model is doing
    - Cyan-themed styling consistent with OpenScientist branding

    Args:
        status_text: Text to display (e.g., "Analyzing literature...",
                    "Searching PubMed...", "Generating report...")

    Returns:
        The container element (can be used to show/hide with .classes())

    Example:
        status = render_thinking_status("Searching databases...")
        status.classes(remove="hidden")  # Show
        status.classes(add="hidden")  # Hide
    """
    _inject_thinking_status_styles()

    with ui.row().classes(
        "items-center gap-3 py-3 px-4 bg-cyan-50 rounded-lg border border-cyan-200"
    ) as container:
        # Animated OpenScientist logo (compact size)
        ui.html(OPENSCIENTIST_THINKING_SVG, sanitize=False).classes("w-6 h-6").style(
            "width:24px;height:24px;min-width:24px;min-height:24px;flex-shrink:0;"
        )
        # Status text
        ui.label(status_text).classes("text-cyan-700 italic thinking-label")

    return container
