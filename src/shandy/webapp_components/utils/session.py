"""Session management utilities for the web application."""

from typing import Any, Dict, List

# Global dict to store uploaded files per session
_uploaded_files: Dict[str, List[Dict[str, Any]]] = {}


def get_uploaded_files(session_id: str) -> List[Dict[str, Any]]:
    """
    Get uploaded files for a session.

    Args:
        session_id: Session identifier

    Returns:
        List of uploaded file dicts with 'name' and 'content' keys
    """
    if session_id not in _uploaded_files:
        _uploaded_files[session_id] = []
    return _uploaded_files[session_id]


def add_uploaded_file(session_id: str, name: str, content: bytes) -> None:
    """
    Add an uploaded file to a session.

    Args:
        session_id: Session identifier
        name: File name
        content: File content as bytes
    """
    if session_id not in _uploaded_files:
        _uploaded_files[session_id] = []

    _uploaded_files[session_id].append({"name": name, "content": content})


def clear_uploaded_files(session_id: str) -> None:
    """
    Clear uploaded files for a session.

    Args:
        session_id: Session identifier
    """
    if session_id in _uploaded_files:
        _uploaded_files[session_id] = []
