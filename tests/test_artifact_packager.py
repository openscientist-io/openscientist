"""Tests for openscientist.artifact_packager module."""

import stat
import zipfile

from openscientist.artifact_packager import create_artifacts_zip, create_artifacts_zip_file


class TestCreateArtifactsZip:
    """Tests for create_artifacts_zip()."""

    def test_creates_valid_zip(self, tmp_path):
        (tmp_path / "report.md").write_text("# Report")
        (tmp_path / "knowledge_state.json").write_text("{}")
        (tmp_path / "config.json").write_text('{"job_id": "j1"}')

        buf = create_artifacts_zip(tmp_path, "j1")

        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            assert "report.md" in names
            assert "knowledge_state.json" in names
            assert "config.json" not in names

    def test_excludes_git_and_pycache(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "mod.pyc").write_bytes(b"\x00")
        (tmp_path / "keep.txt").write_text("keep")

        buf = create_artifacts_zip(tmp_path, "j1")

        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            assert "keep.txt" in names
            assert not any(".git" in n for n in names)
            assert not any("__pycache__" in n for n in names)

    def test_excludes_pytest_cache_and_node_modules(self, tmp_path):
        (tmp_path / ".pytest_cache").mkdir()
        (tmp_path / ".pytest_cache" / "v").write_text("data")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg").write_text("pkg")
        (tmp_path / "data.csv").write_text("a,b")

        buf = create_artifacts_zip(tmp_path, "j1")

        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            assert "data.csv" in names
            assert not any(".pytest_cache" in n for n in names)
            assert not any("node_modules" in n for n in names)

    def test_relative_paths_in_archive(self, tmp_path):
        sub = tmp_path / "data"
        sub.mkdir()
        (sub / "file.csv").write_text("x,y")
        (tmp_path / "report.md").write_text("# R")

        buf = create_artifacts_zip(tmp_path, "j1")

        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            # Paths should be relative to job_dir, using forward slashes
            assert "data/file.csv" in names
            assert "report.md" in names
            # No absolute paths
            assert not any(n.startswith("/") for n in names)

    def test_empty_directory(self, tmp_path):
        buf = create_artifacts_zip(tmp_path, "j1")

        with zipfile.ZipFile(buf) as zf:
            assert zf.namelist() == []

    def test_unreadable_file_skipped(self, tmp_path):
        good = tmp_path / "good.txt"
        good.write_text("good")
        bad = tmp_path / "bad.txt"
        bad.write_text("bad")
        bad.chmod(0o000)

        try:
            buf = create_artifacts_zip(tmp_path, "j1")

            with zipfile.ZipFile(buf) as zf:
                names = zf.namelist()
                assert "good.txt" in names
                # bad.txt should be skipped (logged warning), not crash
        finally:
            # Restore permissions for cleanup
            bad.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_buffer_seeked_to_zero(self, tmp_path):
        (tmp_path / "f.txt").write_text("data")
        buf = create_artifacts_zip(tmp_path, "j1")
        assert buf.tell() == 0

    def test_create_artifacts_zip_file(self, tmp_path):
        (tmp_path / "report.md").write_text("# Report")
        (tmp_path / "config.json").write_text('{"job_id":"j1"}')
        archive_path = tmp_path / "artifacts.zip"

        written = create_artifacts_zip_file(tmp_path, archive_path, "j1")

        assert written == 1
        assert archive_path.exists()
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            assert "report.md" in names
            assert "config.json" not in names
