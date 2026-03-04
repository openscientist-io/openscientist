"""Tests for session management utilities."""

import tempfile
from pathlib import Path

from open_scientist.webapp_components.utils.session import (
    _uploaded_files,
    add_uploaded_file,
    clear_uploaded_files,
    get_uploaded_files,
)


def _make_temp_file(content: bytes = b"test content") -> Path:
    """Create a real temp file for testing."""
    tmp = tempfile.NamedTemporaryFile(delete=False, dir=tempfile.mkdtemp())
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


class TestSessionManagement:
    """Tests for session file management."""

    def setup_method(self):
        """Clear uploaded files before each test."""
        _uploaded_files.clear()

    def test_get_uploaded_files_empty(self):
        """Test getting files for a new session returns empty list."""
        session_id = "test_session_1"
        files = get_uploaded_files(session_id)
        assert files == []
        assert session_id in _uploaded_files

    def test_add_uploaded_file(self):
        """Test adding a file to a session."""
        session_id = "test_session_2"
        file_name = "test.txt"
        file_path = _make_temp_file(b"test content")

        add_uploaded_file(session_id, file_name, file_path)

        files = get_uploaded_files(session_id)
        assert len(files) == 1
        assert files[0]["name"] == file_name
        assert files[0]["path"] == file_path

    def test_add_multiple_files(self):
        """Test adding multiple files to a session."""
        session_id = "test_session_3"

        add_uploaded_file(session_id, "file1.txt", _make_temp_file(b"content1"))
        add_uploaded_file(session_id, "file2.csv", _make_temp_file(b"content2"))
        add_uploaded_file(session_id, "file3.json", _make_temp_file(b"content3"))

        files = get_uploaded_files(session_id)
        assert len(files) == 3
        assert files[0]["name"] == "file1.txt"
        assert files[1]["name"] == "file2.csv"
        assert files[2]["name"] == "file3.json"

    def test_clear_uploaded_files(self):
        """Test clearing files for a session deletes the temp files."""
        session_id = "test_session_4"

        path1 = _make_temp_file(b"content1")
        path2 = _make_temp_file(b"content2")
        add_uploaded_file(session_id, "file1.txt", path1)
        add_uploaded_file(session_id, "file2.txt", path2)

        assert len(get_uploaded_files(session_id)) == 2

        clear_uploaded_files(session_id)

        assert len(get_uploaded_files(session_id)) == 0
        assert not path1.exists()
        assert not path2.exists()

    def test_clear_nonexistent_session(self):
        """Test clearing files for a session that doesn't exist."""
        session_id = "nonexistent_session"
        # Should not raise an error
        clear_uploaded_files(session_id)

    def test_multiple_sessions_isolated(self):
        """Test that multiple sessions keep files isolated."""
        session1 = "session_1"
        session2 = "session_2"

        add_uploaded_file(session1, "file1.txt", _make_temp_file(b"content1"))
        add_uploaded_file(session2, "file2.txt", _make_temp_file(b"content2"))

        files1 = get_uploaded_files(session1)
        files2 = get_uploaded_files(session2)

        assert len(files1) == 1
        assert len(files2) == 1
        assert files1[0]["name"] == "file1.txt"
        assert files2[0]["name"] == "file2.txt"
