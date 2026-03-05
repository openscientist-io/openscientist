"""
Artifact packager for OpenScientist jobs.

Provides utilities for packaging job artifacts (reports, plots, logs, data)
into downloadable archives in various formats (ZIP, Markdown, JSON).
"""

import logging
import zipfile
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

_EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules"}
_EXCLUDE_FILES = {"config.json"}


def _iter_artifact_files(
    job_dir: Path,
    excluded_paths: set[Path] | None = None,
) -> Iterator[tuple[Path, Path]]:
    """Yield (absolute_path, archive_relative_path) pairs for artifact files."""
    excluded_paths = excluded_paths or set()
    for file_path in job_dir.rglob("*"):
        if file_path.resolve() in excluded_paths:
            continue
        if file_path.is_dir():
            continue
        if any(parent.name in _EXCLUDE_DIRS for parent in file_path.parents):
            continue
        if file_path.name in _EXCLUDE_FILES:
            continue
        yield file_path, file_path.relative_to(job_dir)


def _write_artifacts_zip(
    zip_file: zipfile.ZipFile,
    job_dir: Path,
    excluded_paths: set[Path] | None = None,
) -> int:
    """Write job artifacts into an open zip file and return number of files written."""
    written = 0
    for file_path, arcname in _iter_artifact_files(job_dir, excluded_paths=excluded_paths):
        try:
            zip_file.write(file_path, arcname)
            written += 1
        except Exception as e:
            logger.warning("Failed to add %s to archive: %s", arcname, e)
    return written


def create_artifacts_zip(job_dir: Path, job_id: str) -> BytesIO:
    """
    Create a ZIP archive of all job artifacts.

    Includes:
    - Final reports (PDF, Markdown)
    - Plots and visualizations
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
        written = _write_artifacts_zip(zip_file, job_dir)

    zip_buffer.seek(0)
    logger.info(
        "Created artifacts ZIP for job %s (%d files, %d bytes)",
        job_id,
        written,
        zip_buffer.getbuffer().nbytes,
    )

    return zip_buffer


def create_artifacts_zip_file(job_dir: Path, archive_path: Path, job_id: str) -> int:
    """Create an artifacts ZIP archive on disk and return number of files written."""
    excluded_paths: set[Path] = set()
    archive_path_resolved = archive_path.resolve()
    if archive_path_resolved.is_relative_to(job_dir.resolve()):
        excluded_paths.add(archive_path_resolved)

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        written = _write_artifacts_zip(zip_file, job_dir, excluded_paths=excluded_paths)
    logger.info(
        "Created artifacts ZIP file for job %s at %s (%d files)",
        job_id,
        archive_path,
        written,
    )
    return written
