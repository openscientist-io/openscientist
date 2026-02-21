"""
Artifact packager for SHANDY jobs.

Provides utilities for packaging job artifacts (reports, plots, logs, data)
into downloadable archives in various formats (ZIP, Markdown, JSON).
"""

import logging
import zipfile
from io import BytesIO
from pathlib import Path

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
