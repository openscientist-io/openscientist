"""PubMed, job ID and skill name badge rendering for the web app."""

import html
import re

from nicegui import ui


def render_skill_name_slot() -> str:
    """Quasar slot template rendering skill names as links to the detail page."""
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


def _get_pubmed_badge_html(pmid: str) -> str:
    """HTML for a PubMed badge with logo, PMID and a new-tab link."""
    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
    tooltip = f"Visit PubMed page for PMID {pmid}"

    # Inline width/height/style keep the icon sized even if CSS fails to load
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
    """Register CSS and JS for PubMed badges in the app-wide page head.

    Call exactly once at app bootstrap (see ``register_badge_head_html``).
    ``add_head_html(shared=True)`` appends to a process-global string, so
    repeated calls accumulate the block instead of deduplicating.
    """
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
    """Register CSS and JS for job ID badges in the app-wide page head.

    Call exactly once at app bootstrap (same reason as ``_inject_pubmed_badge_styles``).
    """
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


def register_badge_head_html() -> None:
    """Register PubMed/job-ID badge CSS and JS globally for every page.

    Call exactly once at app bootstrap (see ``_inject_pubmed_badge_styles``).
    """
    _inject_pubmed_badge_styles()
    _inject_job_id_badge_styles()


def _get_job_id_badge_html(job_id: str, truncate: bool = True) -> str:
    """HTML for a job ID badge. ``truncate`` shows only the last 8 characters."""
    display_id = job_id[-8:] if truncate and len(job_id) > 8 else job_id
    tooltip = f"View job {job_id}"

    # Inline width/height/style keep the icon sized even if CSS fails to load
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
    """Render a job ID as a badge linking to the job detail page."""
    badge_html = _get_job_id_badge_html(job_id, truncate)
    ui.html(badge_html)


def render_pmid_badge(pmid: str) -> None:
    """Render a standalone PMID as a clickable PubMed badge.

    For PMIDs embedded in text, use ``render_text_with_pmid_links``.
    """
    badge_html = _get_pubmed_badge_html(pmid)
    ui.html(badge_html)


def render_text_with_pmid_links(
    text: str,
    text_classes: str = "text-sm text-gray-700",
) -> None:
    """Render text with PMID references converted to clickable PubMed badges.

    Supports ``PMID:`` and ``PMID`` prefixes with single or comma-separated PMIDs.
    """
    if not text:
        return

    # 5-8 digits avoids matching years (e.g. 2025) as PMIDs
    pattern = re.compile(r"(PMID[:\s]+)(\d{5,8}(?:\s*,\s*\d{5,8})*)", re.IGNORECASE)

    result_parts = []
    last_end = 0

    for match in pattern.finditer(text):
        if match.start() > last_end:
            result_parts.append(html.escape(text[last_end : match.start()]))

        # group(2) holds the PMIDs. The prefix is dropped since badges are self-explanatory
        pmid_list = match.group(2)

        pmids = [p.strip() for p in pmid_list.split(",")]
        pmid_badges = [_get_pubmed_badge_html(pmid) for pmid in pmids]
        result_parts.append(" ".join(pmid_badges))

        last_end = match.end()

    if last_end < len(text):
        result_parts.append(html.escape(text[last_end:]))

    if not result_parts:
        ui.html(
            f'<p class="{text_classes}" style="text-align:justify;hyphens:auto;'
            f'text-justify:inter-word;margin:0;">{html.escape(text)}</p>'
        )
        return

    html_content = "".join(result_parts)
    ui.html(
        f'<p class="{text_classes}" style="text-align:justify;hyphens:auto;'
        f'text-justify:inter-word;margin:0;">{html_content}</p>'
    )


def transform_pmid_references(text: str) -> str:
    """Transform PMID references in text to clickable badge HTML.

    Surrounding text is not HTML-escaped. Only call on content rendered as
    markdown/HTML.
    """
    if not text:
        return text

    # 5-8 digits avoids matching years (e.g. 2025) as PMIDs.
    # The lookbehind skips matches inside markdown links [text](url)
    pattern = re.compile(r"(?<!\[)(PMID[:\s]+)(\d{5,8}(?:\s*,\s*\d{5,8})*)", re.IGNORECASE)

    def replace_pmid(match: re.Match[str]) -> str:
        pmid_list = match.group(2)
        pmids = [p.strip() for p in pmid_list.split(",")]
        badges = [_get_pubmed_badge_html(pmid) for pmid in pmids]
        return " ".join(badges)

    return pattern.sub(replace_pmid, text)
