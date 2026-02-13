"""
Artifact packager for SHANDY jobs.

Provides utilities for packaging job artifacts (reports, plots, logs, data)
into downloadable archives in various formats (ZIP, Markdown, JSON).
"""

import json
import logging
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def create_artifacts_zip(job_dir: Path, job_id: str) -> BytesIO:
    """
    Create a ZIP archive of all job artifacts.

    Includes:
    - Final reports (PDF, Markdown)
    - Plots and visualizations
    - Configuration files
    - Knowledge state
    - Data files
    - Provenance logs

    Args:
        job_dir: Path to job directory
        job_id: Job ID (for logging)

    Returns:
        BytesIO buffer containing ZIP archive
    """
    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        # Add all files recursively, excluding certain directories
        exclude_dirs = {".git", "__pycache__", ".pytest_cache", "node_modules"}

        for file_path in job_dir.rglob("*"):
            # Skip directories and excluded paths
            if file_path.is_dir():
                continue

            # Check if any parent is in exclude list
            if any(parent.name in exclude_dirs for parent in file_path.parents):
                continue

            # Add to archive with relative path
            arcname = file_path.relative_to(job_dir)
            try:
                zip_file.write(file_path, arcname)
            except Exception as e:
                logger.warning("Failed to add %s to archive: %s", arcname, e)

    zip_buffer.seek(0)
    logger.info(
        "Created artifacts ZIP for job %s (%d bytes)",
        job_id,
        zip_buffer.getbuffer().nbytes,
    )

    return zip_buffer


def export_documentation_markdown(job_dir: Path) -> Optional[str]:
    """
    Export job documentation as Markdown.

    Combines research question, findings, hypotheses, literature review,
    and iteration summaries into a comprehensive Markdown document.

    Args:
        job_dir: Path to job directory

    Returns:
        Markdown string, or None if knowledge state not found
    """
    ks_path = job_dir / "knowledge_state.json"
    config_path = job_dir / "config.json"

    if not ks_path.exists():
        logger.warning("Knowledge state not found: %s", ks_path)
        return None

    md_parts = []

    # Header
    md_parts.append("# SHANDY Analysis Documentation\n")
    md_parts.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    # Load config for research question
    if config_path.exists():
        try:
            with open(config_path) as f:
                config = json.load(f)
                research_question = config.get("research_question", "Unknown")
                md_parts.append("## Research Question\n")
                md_parts.append(f"{research_question}\n")
        except Exception as e:
            logger.warning("Failed to load config: %s", e)

    # Load knowledge state
    try:
        with open(ks_path) as f:
            ks = json.load(f)
    except Exception as e:
        logger.error("Failed to load knowledge state: %s", e)
        return None

    # Findings
    findings = ks.get("findings", [])
    if findings:
        md_parts.append("## Key Findings\n")
        for i, finding in enumerate(findings, 1):
            importance = finding.get("importance", "unknown")
            confidence = finding.get("confidence", "unknown")
            md_parts.append(f"### Finding {i}\n")
            md_parts.append(f"**Importance:** {importance} | **Confidence:** {confidence}\n")
            md_parts.append(f"{finding.get('content', '')}\n")

            # Evidence
            evidence = finding.get("evidence", [])
            if evidence:
                md_parts.append("\n**Evidence:**\n")
                for ev in evidence:
                    md_parts.append(f"- {ev}\n")

            # Related hypotheses
            related = finding.get("related_hypothesis_ids", [])
            if related:
                md_parts.append(f"\n**Related Hypotheses:** {', '.join(related)}\n")

            md_parts.append("")

    # Hypotheses
    hypotheses = ks.get("hypotheses", [])
    if hypotheses:
        md_parts.append("## Hypotheses\n")
        for i, hyp in enumerate(hypotheses, 1):
            hyp_id = hyp.get("id", f"H{i}")
            status = hyp.get("status", "unknown")
            md_parts.append(f"### {hyp_id}: {hyp.get('hypothesis', '')}\n")
            md_parts.append(f"**Status:** {status}\n")

            rationale = hyp.get("rationale")
            if rationale:
                md_parts.append(f"\n**Rationale:** {rationale}\n")

            # Tests performed
            tests = hyp.get("tests_performed", [])
            if tests:
                md_parts.append("\n**Tests Performed:**\n")
                for test in tests:
                    md_parts.append(f"- {test}\n")

            md_parts.append("")

    # Literature
    literature = ks.get("literature", [])
    if literature:
        md_parts.append("## Literature Review\n")
        for i, lit in enumerate(literature, 1):
            title = lit.get("title", "Unknown")
            authors = lit.get("authors", "Unknown")
            year = lit.get("year", "Unknown")
            relevance = lit.get("relevance_score", "unknown")

            md_parts.append(f"### {i}. {title}\n")
            md_parts.append(
                f"**Authors:** {authors} | **Year:** {year} | **Relevance:** {relevance}\n"
            )

            # Key findings
            key_findings = lit.get("key_findings", [])
            if key_findings:
                md_parts.append("\n**Key Findings:**\n")
                for kf in key_findings:
                    md_parts.append(f"- {kf}\n")

            # DOI/URL
            doi = lit.get("doi")
            if doi:
                md_parts.append(f"\n**DOI:** {doi}\n")

            md_parts.append("")

    # Iteration summaries
    summaries = ks.get("iteration_summaries", [])
    if summaries:
        md_parts.append("## Analysis Timeline\n")
        for summary in summaries:
            iteration = summary.get("iteration", 0)
            strapline = summary.get("strapline", "")
            summary_text = summary.get("summary", "")
            timestamp = summary.get("timestamp", "")

            md_parts.append(f"### Iteration {iteration}: {strapline}\n")
            if timestamp:
                md_parts.append(f"*{timestamp}*\n")
            md_parts.append(f"{summary_text}\n")
            md_parts.append("")

    return "\n".join(md_parts)


def export_interaction_log_json(job_dir: Path) -> Optional[str]:
    """
    Export interaction log as formatted JSON.

    Includes full analysis log, iteration summaries, and feedback history.

    Args:
        job_dir: Path to job directory

    Returns:
        JSON string, or None if knowledge state not found
    """
    ks_path = job_dir / "knowledge_state.json"

    if not ks_path.exists():
        logger.warning("Knowledge state not found: %s", ks_path)
        return None

    try:
        with open(ks_path) as f:
            ks = json.load(f)
    except Exception as e:
        logger.error("Failed to load knowledge state: %s", e)
        return None

    # Extract relevant parts
    export_data = {
        "exported_at": datetime.now(timezone.utc).isoformat() + "Z",
        "iteration": ks.get("iteration", 0),
        "analysis_log": ks.get("analysis_log", []),
        "iteration_summaries": ks.get("iteration_summaries", []),
        "feedback_history": ks.get("feedback_history", []),
    }

    return json.dumps(export_data, indent=2)


def export_interaction_log_markdown(job_dir: Path) -> Optional[str]:
    """
    Export interaction log as Markdown.

    Human-readable format showing the chronological analysis process.

    Args:
        job_dir: Path to job directory

    Returns:
        Markdown string, or None if knowledge state not found
    """
    ks_path = job_dir / "knowledge_state.json"

    if not ks_path.exists():
        logger.warning("Knowledge state not found: %s", ks_path)
        return None

    try:
        with open(ks_path) as f:
            ks = json.load(f)
    except Exception as e:
        logger.error("Failed to load knowledge state: %s", e)
        return None

    md_parts = []

    # Header
    md_parts.append("# SHANDY Interaction Log\n")
    md_parts.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    # Analysis log grouped by iteration
    analysis_log = ks.get("analysis_log", [])
    if analysis_log:
        # Group by iteration
        from collections import defaultdict

        by_iteration = defaultdict(list)
        for entry in analysis_log:
            by_iteration[entry["iteration"]].append(entry)

        # Display chronologically
        for iteration in sorted(by_iteration.keys()):
            md_parts.append(f"## Iteration {iteration}\n")

            # Get iteration summary if available
            summaries = ks.get("iteration_summaries", [])
            iteration_summary = next(
                (s for s in summaries if s.get("iteration") == iteration), None
            )
            if iteration_summary:
                strapline = iteration_summary.get("strapline", "")
                if strapline:
                    md_parts.append(f"**{strapline}**\n")

            # Log entries
            for entry in by_iteration[iteration]:
                action = entry.get("action", "unknown")
                timestamp = entry.get("timestamp", "")
                details = entry.get("details", "")

                md_parts.append(f"### {action}\n")
                if timestamp:
                    md_parts.append(f"*{timestamp}*\n")
                md_parts.append(f"{details}\n")

            md_parts.append("")

    # Feedback history
    feedback = ks.get("feedback_history", [])
    if feedback:
        md_parts.append("## Scientist Feedback\n")
        for fb in feedback:
            iteration = fb.get("iteration", 0)
            feedback_text = fb.get("feedback", "")
            timestamp = fb.get("timestamp", "")

            md_parts.append(f"### Iteration {iteration}\n")
            if timestamp:
                md_parts.append(f"*{timestamp}*\n")
            md_parts.append(f"{feedback_text}\n")
            md_parts.append("")

    return "\n".join(md_parts)


def get_report_path(job_dir: Path) -> Optional[Path]:
    """
    Find the final report file (PDF or Markdown).

    Args:
        job_dir: Path to job directory

    Returns:
        Path to report file, or None if not found
    """
    # Try different possible report names
    report_candidates = [
        job_dir / "final_report.pdf",
        job_dir / "report.pdf",
        job_dir / "final_report.md",
        job_dir / "report.md",
    ]

    for candidate in report_candidates:
        if candidate.exists():
            return candidate

    return None
