"""
File loading and type detection for SHANDY.

Handles multiple file formats with validation and magic number detection.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

# Try to import python-magic, but make it optional
try:
    import magic

    HAS_MAGIC = True
except (ImportError, OSError):
    HAS_MAGIC = False

logger = logging.getLogger(__name__)

# File size limits (in bytes) - configurable via environment variable
# Default: 1GB
_default_max_file_size_mb = 1000
_env_max_file_size = os.getenv("MAX_FILE_SIZE_MB")
if _env_max_file_size:
    try:
        MAX_FILE_SIZE = int(_env_max_file_size) * 1024 * 1024
    except ValueError:
        logger.warning(
            f"Invalid MAX_FILE_SIZE_MB value '{_env_max_file_size}', using default {_default_max_file_size_mb}MB"
        )
        MAX_FILE_SIZE = _default_max_file_size_mb * 1024 * 1024
else:
    MAX_FILE_SIZE = _default_max_file_size_mb * 1024 * 1024

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


class FileTooBigError(Exception):
    """Raised when file exceeds size limit."""

    pass


class UnsupportedFileTypeError(Exception):
    """Raised when file type is not supported."""

    pass


def get_file_info(file_path: Path) -> Dict[str, Any]:
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
    if size > MAX_FILE_SIZE:
        raise FileTooBigError(
            f"File {file_path.name} is {size / 1024 / 1024:.1f}MB, "
            f"exceeds limit of {MAX_FILE_SIZE / 1024 / 1024}MB"
        )

    # Get extension
    extension = file_path.suffix.lower()

    # Detect MIME type using python-magic (if available)
    if HAS_MAGIC:
        try:
            mime_type = magic.from_file(str(file_path), mime=True)
        except Exception as e:
            logger.warning(f"Could not detect MIME type for {file_path}: {e}")
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
        elif extension == ".tsv":
            return pd.read_csv(file_path, sep="\t")

        # Excel
        elif extension in [".xlsx", ".xls"]:
            return pd.read_excel(file_path)

        # Parquet
        elif extension in [".parquet", ".pq"]:
            return pd.read_parquet(file_path)

        # JSON
        elif extension == ".json":
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

    except Exception as e:
        logger.error(f"Failed to load {file_path}: {e}")
        raise ValueError(f"Could not parse {file_path.name} as {extension}: {e}")


def load_data_file(file_path: Path) -> Optional[pd.DataFrame]:
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
        logger.info(f"Loading tabular file: {file_path.name} ({info['extension']})")
        return load_tabular_file(file_path)

    elif info["file_type"] in ["structure", "sequence"]:
        logger.info(
            f"Non-tabular file detected: {file_path.name} ({info['file_type']}). "
            f"Agent will access file directly."
        )
        return None

    else:
        logger.warning(
            f"Unknown file type: {file_path.name} ({info['extension']}, {info['mime_type']}). "
            f"Agent will attempt to handle it."
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
    if len(content) > MAX_FILE_SIZE:
        raise FileTooBigError(
            f"File is {len(content) / 1024 / 1024:.1f}MB, "
            f"exceeds limit of {MAX_FILE_SIZE / 1024 / 1024}MB"
        )

    # Get extension
    extension = file_path.suffix.lower()

    # Detect actual file type from content
    try:
        mime_type = magic.from_buffer(content, mime=True)
    except Exception as e:
        logger.warning(f"Could not detect MIME type from content: {e}")
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

    if extension in expected_mimes:
        if not any(exp in mime_type for exp in expected_mimes[extension]):
            logger.warning(
                f"File extension {extension} doesn't match detected type {mime_type}. "
                f"Proceeding anyway, but this may indicate file corruption."
            )

    logger.info(f"File validation passed: {file_path.name} ({mime_type}, {len(content)} bytes)")
