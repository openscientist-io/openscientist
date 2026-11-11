"""Session management utilities for the web application."""

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import TypedDict

from nicegui.elements.upload_files import FileUpload

logger = logging.getLogger(__name__)


class UploadedFile(TypedDict):
    name: str
    path: Path


_uploaded_files: dict[str, list[UploadedFile]] = {}
_upload_locks: dict[str, asyncio.Lock] = {}


def get_uploaded_files(session_id: str) -> list[UploadedFile]:
    """
    Get uploaded files for a session.

    Args:
        session_id: Session identifier

    Returns:
        List of uploaded file dicts with 'name' and 'path' keys
    """
    if session_id not in _uploaded_files:
        _uploaded_files[session_id] = []
    return _uploaded_files[session_id]


def add_uploaded_file(session_id: str, name: str, path: Path) -> None:
    """
    Add an uploaded file (already saved to disk) to a session.

    Args:
        session_id: Session identifier
        name: Original file name
        path: Path to the temp file on disk
    """
    if session_id not in _uploaded_files:
        _uploaded_files[session_id] = []

    _uploaded_files[session_id].append({"name": name, "path": path})


def clear_uploaded_files(session_id: str) -> None:
    """
    Clear uploaded files for a session and delete their temp files.

    Args:
        session_id: Session identifier
    """
    _upload_locks.pop(session_id, None)
    for uploaded_file in _uploaded_files.pop(session_id, []):
        try:
            uploaded_file["path"].unlink(missing_ok=True)
            uploaded_file["path"].parent.rmdir()
        except OSError:
            logger.debug("Could not remove the temp file for session %s", session_id, exc_info=True)


def upload_storage_root() -> Path:
    """Return the web process's private upload staging directory."""
    return Path(os.environ.get("NICEGUI_STORAGE_PATH", ".nicegui")) / "uploads"


def clear_upload_storage() -> None:
    """Remove upload staging files on web startup and shutdown."""
    for session_id in tuple(_uploaded_files):
        clear_uploaded_files(session_id)
    root = upload_storage_root()
    if root.exists():
        shutil.rmtree(root)


async def save_uploaded_file(
    session_id: str,
    file: FileUpload,
    *,
    max_files: int,
    max_file_size_bytes: int,
) -> bool:
    """Save an upload within its page quota, unless the page disconnects."""
    files = get_uploaded_files(session_id)
    lock = _upload_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        if _uploaded_files.get(session_id) is not files:
            return False
        if len(files) >= max_files:
            raise ValueError(f"Upload at most {max_files} files per page")
        total_limit = max_file_size_bytes * max_files
        if sum(entry["path"].stat().st_size for entry in files) + file.size() > total_limit:
            raise ValueError(
                f"Uploads exceed the total limit of {total_limit // (1024 * 1024)} MB per page"
            )
        root = upload_storage_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="file-", dir=root))
        retained = False
        try:
            target = directory / Path(file.name).name
            await file.save(target)
            if _uploaded_files.get(session_id) is not files:
                return False
            add_uploaded_file(session_id, file.name, target)
            retained = True
            return True
        finally:
            if not retained:
                shutil.rmtree(directory)
