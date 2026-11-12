"""Session management utilities for the web application."""

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from nicegui.elements.upload_files import FileUpload

logger = logging.getLogger(__name__)


class UploadedFile(TypedDict):
    name: str
    path: Path
    size: int
    key: str | None


@dataclass(eq=False)
class PendingUpload:
    name: str
    files: list[UploadedFile]
    key: str | None = None
    size: int = 0
    claimed: bool = False
    cancelled: bool = False
    finished: bool = False


_uploaded_files: dict[str, list[UploadedFile]] = {}
_upload_locks: dict[str, asyncio.Lock] = {}
_pending_uploads: dict[str, list[PendingUpload]] = {}


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


def add_uploaded_file(session_id: str, name: str, path: Path, key: str | None = None) -> None:
    """
    Add an uploaded file (already saved to disk) to a session.

    Args:
        session_id: Session identifier
        name: Original file name
        path: Path to the temp file on disk
    """
    if session_id not in _uploaded_files:
        _uploaded_files[session_id] = []

    _uploaded_files[session_id].append(
        {
            "name": name,
            "path": path,
            "size": path.stat().st_size,
            "key": key,
        }
    )


def clear_uploaded_files(session_id: str) -> None:
    """
    Clear uploaded files for a session and delete their temp files.

    Args:
        session_id: Session identifier
    """
    for upload in tuple(_pending_uploads.get(session_id, [])):
        upload.cancelled = True
        if upload.finished:
            finish_upload(session_id, upload)
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
    upload: PendingUpload,
    *,
    max_files: int,
    max_file_size_bytes: int,
) -> bool:
    """Save an upload within its page quota, unless the page disconnects."""
    files = upload.files
    if upload.cancelled or _uploaded_files.get(session_id) is not files:
        return False
    lock = _upload_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        if upload.cancelled or _uploaded_files.get(session_id) is not files:
            return False
        if len(files) >= max_files:
            raise ValueError(f"Upload at most {max_files} files per page")
        total_limit = max_file_size_bytes * max_files
        if sum(entry["size"] for entry in files) + file.size() > total_limit:
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
            if upload.cancelled or _uploaded_files.get(session_id) is not files:
                return False
            add_uploaded_file(session_id, file.name, target, upload.key)
            retained = True
            return True
        finally:
            if not retained:
                shutil.rmtree(directory)


def check_upload_quota(session_id: str, max_files: int, max_file_size_bytes: int) -> None:
    """Bound page entries and bytes, including concurrent uploads."""
    files = _uploaded_files.get(session_id, [])
    pending = _pending_uploads.get(session_id, [])
    if len(files) + len(pending) > max_files:
        raise ValueError(f"Upload at most {max_files} files per page")
    total = sum(entry["size"] for entry in files) + sum(
        upload.size for upload in pending if not upload.finished
    )
    total_limit = max_file_size_bytes * max_files
    if total > total_limit:
        raise ValueError(
            f"Uploads exceed the total limit of {total_limit // (1024 * 1024)} MB per page"
        )


def reserve_upload(session_id: str, name: str, key: str | None = None) -> PendingUpload:
    """Reserve a unique sanitized filename before receiving its body."""
    files = get_uploaded_files(session_id)
    pending = _pending_uploads.setdefault(session_id, [])
    if any(entry["name"] == name for entry in files) or any(
        entry.name == name for entry in pending
    ):
        raise ValueError(f"{name} is already uploaded or in progress")
    upload = PendingUpload(name, files, key)
    pending.append(upload)
    return upload


def begin_upload(session_id: str, file: FileUpload) -> PendingUpload:
    """Claim the multipart reservation before scheduling the save."""
    upload = next(
        (
            entry
            for entry in _pending_uploads.get(session_id, [])
            if entry.name == file.name and not entry.claimed
        ),
        None,
    )
    if upload is None:
        upload = reserve_upload(session_id, file.name)
    upload.claimed = True
    upload.size = file.size()
    return upload


def finish_upload(session_id: str, upload: PendingUpload) -> None:
    """Keep acknowledged failed names until their browser entries are removed."""
    upload.finished = True
    if (
        upload.claimed
        and not upload.cancelled
        and not any(entry["name"] == upload.name for entry in upload.files)
    ):
        return
    pending = _pending_uploads.get(session_id)
    if pending is not None and upload in pending:
        pending.remove(upload)
        if not pending:
            _pending_uploads.pop(session_id)


def remove_uploaded_files(session_id: str, removed: list[str]) -> None:
    """Remove matching file keys from this page and cancel their pending saves."""
    files = _uploaded_files.get(session_id, [])
    pending = _pending_uploads.get(session_id, [])
    for key in removed:
        for upload in tuple(pending):
            if upload.key == key:
                upload.cancelled = True
                if upload.finished:
                    finish_upload(session_id, upload)
        stored = next((entry for entry in files if entry["key"] == key), None)
        if stored is not None:
            stored["path"].unlink(missing_ok=True)
            stored["path"].parent.rmdir()
            files.remove(stored)
