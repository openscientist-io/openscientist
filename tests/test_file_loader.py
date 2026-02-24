"""Tests for file_loader module."""

import json
from unittest.mock import patch

import pandas as pd
import pytest

from shandy.file_loader import (
    FileTooBigError,
    UnsupportedFileTypeError,
    get_file_info,
    load_data_file,
    load_tabular_file,
    validate_uploaded_file,
)

# ─── get_file_info ────────────────────────────────────────────────────


class TestGetFileInfo:
    """Tests for file metadata extraction."""

    def test_csv_file(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("a,b\n1,2\n")
        info = get_file_info(p)
        assert info["extension"] == ".csv"
        assert info["file_type"] == "tabular"
        assert info["size"] > 0
        assert info["name"] == "data.csv"

    def test_pdb_file(self, tmp_path):
        p = tmp_path / "protein.pdb"
        p.write_text("ATOM  1  CA  ALA A   1\n")
        info = get_file_info(p)
        assert info["file_type"] == "structure"

    def test_fasta_file(self, tmp_path):
        p = tmp_path / "seq.fasta"
        p.write_text(">seq1\nATCG\n")
        info = get_file_info(p)
        assert info["file_type"] == "sequence"

    def test_png_file(self, tmp_path):
        p = tmp_path / "image.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        info = get_file_info(p)
        assert info["file_type"] == "image"

    def test_unknown_extension(self, tmp_path):
        p = tmp_path / "mystery.xyz"
        p.write_text("something\n")
        info = get_file_info(p)
        assert info["file_type"] == "unknown"

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            get_file_info(tmp_path / "nonexistent.csv")

    def test_file_too_big(self, tmp_path):
        p = tmp_path / "big.csv"
        p.write_text("x" * 100)
        with (
            patch("shandy.file_loader._get_max_file_size", return_value=10),
            pytest.raises(
                FileTooBigError,
                match="exceeds limit",
            ),
        ):
            get_file_info(p)


# ─── load_tabular_file ────────────────────────────────────────────────


class TestLoadTabularFile:
    """Tests for tabular file loading."""

    def test_load_csv(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("col_a,col_b\n1,2\n3,4\n")
        df = load_tabular_file(p)
        assert list(df.columns) == ["col_a", "col_b"]
        assert len(df) == 2

    def test_load_tsv(self, tmp_path):
        p = tmp_path / "data.tsv"
        p.write_text("col_a\tcol_b\n1\t2\n3\t4\n")
        df = load_tabular_file(p)
        assert list(df.columns) == ["col_a", "col_b"]
        assert len(df) == 2

    def test_load_json_records(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text(json.dumps([{"a": 1, "b": 2}, {"a": 3, "b": 4}]))
        df = load_tabular_file(p)
        assert len(df) == 2
        assert "a" in df.columns

    def test_load_jsonl(self, tmp_path):
        p = tmp_path / "data.jsonl"
        p.write_text('{"a":1}\n{"a":2}\n')
        df = load_tabular_file(p)
        assert len(df) == 2

    def test_load_parquet(self, tmp_path):
        p = tmp_path / "data.parquet"
        df_orig = pd.DataFrame({"x": [1, 2, 3]})
        df_orig.to_parquet(p)
        df = load_tabular_file(p)
        assert list(df.columns) == ["x"]
        assert len(df) == 3

    def test_unsupported_extension(self, tmp_path):
        p = tmp_path / "data.hdf5"
        p.write_bytes(b"\x00" * 10)
        with pytest.raises(UnsupportedFileTypeError, match="not supported"):
            load_tabular_file(p)

    def test_unsupported_tabular_extension(self, tmp_path):
        p = tmp_path / "data.weirdformat"
        p.write_text("a,b\n1,2\n")
        with pytest.raises(UnsupportedFileTypeError, match="not supported"):
            load_tabular_file(p)


# ─── load_data_file ───────────────────────────────────────────────────


class TestLoadDataFile:
    """Tests for the top-level load_data_file dispatcher."""

    def test_tabular_returns_dataframe(self, tmp_path):
        p = tmp_path / "data.csv"
        p.write_text("a,b\n1,2\n")
        result = load_data_file(p)
        assert isinstance(result, pd.DataFrame)

    def test_structure_file_returns_none(self, tmp_path):
        p = tmp_path / "model.pdb"
        p.write_text("ATOM  1  CA  ALA A   1\n")
        result = load_data_file(p)
        assert result is None

    def test_sequence_file_returns_none(self, tmp_path):
        p = tmp_path / "reads.fastq"
        p.write_text("@read1\nATCG\n+\nIIII\n")
        result = load_data_file(p)
        assert result is None

    def test_unknown_type_returns_none(self, tmp_path):
        p = tmp_path / "unknown.abc"
        p.write_text("data")
        result = load_data_file(p)
        assert result is None


# ─── validate_uploaded_file ───────────────────────────────────────────


class TestValidateUploadedFile:
    """Tests for upload validation."""

    def test_valid_csv_passes(self, tmp_path):
        content = b"a,b\n1,2\n"
        validate_uploaded_file(tmp_path / "data.csv", content)

    def test_file_too_big_raises(self, tmp_path):
        content = b"x" * 100
        with (
            patch("shandy.file_loader._get_max_file_size", return_value=10),
            pytest.raises(
                FileTooBigError,
            ),
        ):
            validate_uploaded_file(tmp_path / "data.csv", content)

    def test_executable_content_raises(self, tmp_path):
        """Executable MIME types should be rejected."""
        with patch("shandy.file_loader.magic") as mock_magic:
            mock_magic.from_buffer.return_value = "application/x-executable"
            with pytest.raises(ValueError, match="Executable file detected"):
                validate_uploaded_file(tmp_path / "data.csv", b"fake-binary")
