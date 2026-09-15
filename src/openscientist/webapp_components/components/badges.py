"""
Badge UI components for OpenScientist web interface.

Provides badge/pill-style rendering helpers (job status, job ID, PubMed
PMID references, skill categories, container status) used throughout the
NiceGUI web interface.
"""

import html
import re
from typing import Any

from nicegui import ui

from openscientist.job.types import JobStatus

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
        "Agent": "smart_toy",
        "Provider": "cloud",
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


def register_badge_head_html() -> None:
    """Register PubMed/job-id badge CSS and JS globally, for every page.

    Call exactly once, at app bootstrap (alongside ``_register_pwa_metadata``
    in ``web_app._configure_host_app``) -- see ``_inject_pubmed_badge_styles``
    for why this can't be called from per-render code.
    """
    _inject_pubmed_badge_styles()
    _inject_job_id_badge_styles()
