"""
File loading and type detection for SHANDY.

Handles multiple file formats with validation and magic number detection.
"""

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from shandy.exceptions import FileLoadError, FileTooBigError, UnsupportedFileTypeError
from shandy.settings import get_settings

# Try to import python-magic, but make it optional
try:
    import magic

    HAS_MAGIC = True
except (ImportError, OSError):
    HAS_MAGIC = False

logger = logging.getLogger(__name__)


def _get_max_file_size() -> int:
    """Get max file size in bytes from settings."""
    return get_settings().file.max_file_size_mb * 1024 * 1024


# Supported file extensions
TABULAR_EXTENSIONS = {
    ".csv",
    ".tsv",
    ".txt",
    ".xlsx",
    ".xls",
    ".parquet",
    ".pq",
    ".json",
    ".jsonl",
    ".feather",
}

STRUCTURE_EXTENSIONS = {
    ".pdb",
    ".cif",
    ".ent",
    ".mmcif",
    ".pdbqt",
    ".mol2",
    ".sdf",
}

SEQUENCE_EXTENSIONS = {
    ".fasta",
    ".fa",
    ".fna",
    ".faa",
    ".fastq",
    ".fq",
    ".gb",
    ".gbk",
    ".genbank",
}

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".svg",
    ".webp",
}


# Re-exported from shandy.exceptions for convenience at this import path
__all__ = ["FileTooBigError", "UnsupportedFileTypeError"]


def get_file_info(file_path: Path) -> dict[str, Any]:
    """
    Get comprehensive file information.

    Args:
        file_path: Path to file

    Returns:
        Dictionary with file metadata including:
        - size: File size in bytes
        - extension: File extension
        - mime_type: MIME type from magic numbers
        - file_type: Detected file category (tabular, structure, sequence, unknown)

    Raises:
        FileNotFoundError: If file doesn't exist
        FileTooBigError: If file exceeds size limit
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # Check file size
    size = file_path.stat().st_size
    if size > _get_max_file_size():
        raise FileTooBigError(
            f"File {file_path.name} is {size / 1024 / 1024:.1f}MB, "
            f"exceeds limit of {_get_max_file_size() / 1024 / 1024}MB"
        )

    # Get extension
    extension = file_path.suffix.lower()

    # Detect MIME type using python-magic (if available)
    if HAS_MAGIC:
        try:
            mime_type = magic.from_file(str(file_path), mime=True)
        except (ValueError, OSError) as e:
            logger.warning("Could not detect MIME type for %s: %s", file_path, e)
            mime_type = "application/octet-stream"
    else:
        # Fall back to extension-based detection
        mime_type = "application/octet-stream"

    # Determine file category
    if extension in TABULAR_EXTENSIONS:
        file_type = "tabular"
    elif extension in STRUCTURE_EXTENSIONS:
        file_type = "structure"
    elif extension in SEQUENCE_EXTENSIONS:
        file_type = "sequence"
    elif extension in IMAGE_EXTENSIONS:
        file_type = "image"
    else:
        file_type = "unknown"

    return {
        "path": str(file_path),
        "name": file_path.name,
        "size": size,
        "extension": extension,
        "mime_type": mime_type,
        "file_type": file_type,
    }


def load_tabular_file(file_path: Path) -> pd.DataFrame:
    """
    Load tabular data file into pandas DataFrame.

    Supports: CSV, TSV, Excel, Parquet, JSON, Feather

    Args:
        file_path: Path to data file

    Returns:
        DataFrame with loaded data

    Raises:
        UnsupportedFileTypeError: If file type is not supported
        ValueError: If file cannot be parsed
    """
    extension = file_path.suffix.lower()

    try:
        # CSV and TSV - use auto-detection for delimiter
        if extension in [".csv", ".txt"]:
            return pd.read_csv(file_path, sep=None, engine="python")
        if extension == ".tsv":
            return pd.read_csv(file_path, sep="\t")

        # Excel
        if extension in [".xlsx", ".xls"]:
            return pd.read_excel(file_path)

        # Parquet
        if extension in [".parquet", ".pq"]:
            return pd.read_parquet(file_path)

        # JSON
        if extension == ".json":
            # Try reading as records first (common for data), fallback to lines
            try:
                return pd.read_json(file_path, orient="records")
            except ValueError:
                return pd.read_json(file_path, lines=False)

        elif extension == ".jsonl":
            return pd.read_json(file_path, lines=True)

        # Feather
        elif extension == ".feather":
            return pd.read_feather(file_path)

        else:
            raise UnsupportedFileTypeError(
                f"Tabular file type {extension} not supported. "
                f"Supported: {', '.join(sorted(TABULAR_EXTENSIONS))}"
            )

    except (ValueError, OSError, pd.errors.ParserError, KeyError) as e:
        logger.error("Failed to load %s: %s", file_path, e)
        raise FileLoadError(f"Could not parse {file_path.name} as {extension}: {e}") from e


def load_data_file(file_path: Path) -> pd.DataFrame | None:
    """
    Load data file, returning DataFrame for tabular data or None for other types.

    For non-tabular files (structures, sequences), returns None and the file
    should be accessed directly by the agent via execute_code.

    Args:
        file_path: Path to data file

    Returns:
        DataFrame if tabular data, None otherwise

    Raises:
        FileTooBigError: If file exceeds size limit
    """
    info = get_file_info(file_path)

    if info["file_type"] == "tabular":
        logger.info("Loading tabular file: %s (%s)", file_path.name, info["extension"])
        return load_tabular_file(file_path)

    if info["file_type"] in ["structure", "sequence"]:
        logger.info(
            "Non-tabular file detected: %s (%s). Agent will access file directly.",
            file_path.name,
            info["file_type"],
        )
        return None

    logger.warning(
        "Unknown file type: %s (%s, %s). Agent will attempt to handle it.",
        file_path.name,
        info["extension"],
        info["mime_type"],
    )
    return None


def validate_uploaded_file(file_path: Path, content: bytes) -> None:
    """
    Validate uploaded file for security.

    Checks:
    - File size
    - File extension vs. magic number consistency
    - No executable content

    Args:
        file_path: Path where file will be saved
        content: File content bytes

    Raises:
        FileTooBigError: If file too large
        ValueError: If file fails validation
    """
    # Check size
    if len(content) > _get_max_file_size():
        raise FileTooBigError(
            f"File is {len(content) / 1024 / 1024:.1f}MB, "
            f"exceeds limit of {_get_max_file_size() / 1024 / 1024}MB"
        )

    # Get extension
    extension = file_path.suffix.lower()

    # Detect actual file type from content
    try:
        mime_type = magic.from_buffer(content, mime=True)
    except (ValueError, OSError) as e:
        logger.warning("Could not detect MIME type from content: %s", e)
        mime_type = "application/octet-stream"

    # Check for executable content
    dangerous_mimes = [
        "application/x-executable",
        "application/x-sharedlib",
        "application/x-mach-binary",
        "application/x-dosexec",
    ]

    if any(dangerous in mime_type for dangerous in dangerous_mimes):
        raise ValueError(
            f"Executable file detected (MIME: {mime_type}). Only data files are allowed."
        )

    # Warn if extension doesn't match content
    expected_mimes = {
        ".csv": ["text/plain", "text/csv", "application/csv"],
        ".tsv": ["text/plain", "text/tab-separated-values"],
        ".xlsx": ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
        ".xls": ["application/vnd.ms-excel"],
        ".json": ["application/json", "text/plain"],
        ".pdb": ["text/plain", "chemical/x-pdb"],
        ".cif": ["text/plain", "chemical/x-cif"],
    }

    if extension in expected_mimes and not any(
        exp in mime_type for exp in expected_mimes[extension]
    ):
        logger.warning(
            "File extension %s doesn't match detected type %s. "
            "Proceeding anyway, but this may indicate file corruption.",
            extension,
            mime_type,
        )

    logger.info(
        "File validation passed: %s (%s, %d bytes)",
        file_path.name,
        mime_type,
        len(content),
    )
