"""
Artifact packager for OpenScientist jobs.

Provides utilities for packaging job artifacts (reports, plots, logs, data)
into downloadable archives in various formats (ZIP, Markdown, JSON).
"""

import json
import logging
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

EXCLUDED_FILES_MANIFEST = "EXCLUDED_FILES.txt"

# The run's recorded environment, written from the database at the archive root.
# The job directory is agent-writable, so a file there under this name is never
# shipped in its place.
PROVENANCE_FILE = "provenance.json"

# Agent working directories are never artifacts, and they hold credentials: the
# per-job MCP config, session transcripts, and the copied omp credential vault.
_EXCLUDE_DIRS = {
    ".codex",
    ".omp",
    ".omp-home",
    ".git",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
}
_EXCLUDE_FILES = {"config.json", EXCLUDED_FILES_MANIFEST}

MAX_ARTIFACT_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB


def _shown_dir(dir_path: Path) -> bool:
    """Whether a directory of a job may hold artifacts. Linked directories are
    never followed, since they could lead outside the job directory."""
    return dir_path.name not in _EXCLUDE_DIRS and not dir_path.is_symlink()


def _shown_file(file_path: Path) -> bool:
    """Whether a file of a job is an artifact. Job directories are agent-writable,
    so a file link could smuggle credentials or files from elsewhere in."""
    return file_path.name not in _EXCLUDE_FILES and not file_path.is_symlink()


def _iter_artifact_files(
    job_dir: Path,
    excluded_paths: set[Path] | None = None,
) -> Iterator[tuple[Path, Path]]:
    """Yield (absolute_path, archive_relative_path) pairs for artifact files."""
    excluded_paths = excluded_paths or set()
    for root, dirnames, filenames in job_dir.walk(top_down=True, follow_symlinks=False):
        # Prune before descent so linked directories are never traversed.
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if _shown_dir(root / dirname) and (root / dirname).resolve() not in excluded_paths
        ]
        for filename in filenames:
            file_path = root / filename
            if root == job_dir and filename == PROVENANCE_FILE:
                continue
            if _shown_file(file_path) and file_path.resolve() not in excluded_paths:
                yield file_path, file_path.relative_to(job_dir)


def artifact_file(job_dir: Path, relative: str) -> Path | None:
    """The file at ``relative`` in ``job_dir`` when the artifacts ZIP would
    include it, or None."""
    parts = PurePosixPath(relative).parts
    if not parts or any(part in ("", ".", "..", "/") or "\\" in part for part in parts):
        return None
    if parts == (PROVENANCE_FILE,):
        return None
    path = job_dir
    for dirname in parts[:-1]:
        path = path / dirname
        if not _shown_dir(path) or not path.is_dir():
            return None
    path = path / parts[-1]
    if not _shown_file(path) or not path.is_file():
        return None
    if path.stat().st_size > MAX_ARTIFACT_FILE_SIZE_BYTES:
        return None
    return path


def _partition_by_size(
    files: Iterator[tuple[Path, Path]],
) -> tuple[list[tuple[Path, Path]], list[tuple[Path, int]]]:
    included: list[tuple[Path, Path]] = []
    oversized: list[tuple[Path, int]] = []
    for file_path, arcname in files:
        try:
            size = file_path.stat().st_size
        except OSError:
            included.append((file_path, arcname))
            continue
        if size > MAX_ARTIFACT_FILE_SIZE_BYTES:
            oversized.append((arcname, size))
        else:
            included.append((file_path, arcname))
    return included, oversized


def _format_excluded_manifest(oversized: list[tuple[Path, int]]) -> str:
    lines = [
        "The following files were left out of this archive because they exceed "
        f"{MAX_ARTIFACT_FILE_SIZE_BYTES // (1024 * 1024)} MB. Large files here are "
        "often reference datasets used as analysis input rather than report "
        "outputs; check the job's data sources if you need one of these.",
        "",
    ]
    for arcname, size in sorted(oversized, key=lambda pair: pair[1], reverse=True):
        lines.append(f"{size / (1024 * 1024):>10.1f} MB  {arcname.as_posix()}")
    return "\n".join(lines) + "\n"


def _write_artifacts_zip(
    zip_file: zipfile.ZipFile,
    job_dir: Path,
    excluded_paths: set[Path] | None = None,
) -> int:
    """Write job artifacts into an open zip file and return number of files written."""
    included, oversized = _partition_by_size(
        _iter_artifact_files(job_dir, excluded_paths=excluded_paths)
    )

    written = 0
    for file_path, arcname in included:
        try:
            zip_file.write(file_path, arcname)
            written += 1
        except Exception as e:
            logger.warning("Failed to add %s to archive: %s", arcname, e)

    if oversized:
        zip_file.writestr(EXCLUDED_FILES_MANIFEST, _format_excluded_manifest(oversized))
        logger.info(
            "Excluded %d oversized file(s) from artifacts archive (over %d MB): %s",
            len(oversized),
            MAX_ARTIFACT_FILE_SIZE_BYTES // (1024 * 1024),
            ", ".join(arcname.as_posix() for arcname, _ in oversized),
        )

    return written


def create_artifacts_zip_file(
    job_dir: Path,
    archive_path: Path,
    job_id: str,
    version_info: dict[str, str] | None = None,
) -> int:
    """Create an artifacts ZIP archive on disk and return number of files written.

    ``version_info`` is the environment the run recorded, written as
    ``provenance.json`` when the run recorded one."""
    excluded_paths: set[Path] = set()
    archive_path_resolved = archive_path.resolve()
    if archive_path_resolved.is_relative_to(job_dir.resolve()):
        excluded_paths.add(archive_path_resolved)

    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        written = _write_artifacts_zip(zip_file, job_dir, excluded_paths=excluded_paths)
        if version_info:
            zip_file.writestr(
                PROVENANCE_FILE, json.dumps(version_info, indent=2, sort_keys=True) + "\n"
            )
            written += 1
    logger.info(
        "Created artifacts ZIP file for job %s at %s (%d files)",
        job_id,
        archive_path,
        written,
    )
    return written
