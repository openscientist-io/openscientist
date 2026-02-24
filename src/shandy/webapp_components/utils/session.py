"""Session management utilities for the web application."""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Global dict to store uploaded files per session (keyed by session_id)
# Each entry is a list of {"name": str, "path": Path} dicts.
_uploaded_files: dict[str, list[dict[str, Any]]] = {}


def get_uploaded_files(session_id: str) -> list[dict[str, Any]]:
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
    for uploaded_file in _uploaded_files.get(session_id, []):
        try:
            uploaded_file["path"].unlink(missing_ok=True)
            uploaded_file["path"].parent.rmdir()
        except Exception:
            pass
    if session_id in _uploaded_files:
        _uploaded_files[session_id] = []
