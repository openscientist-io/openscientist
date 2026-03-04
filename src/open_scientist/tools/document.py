"""
Document reading tool for the SDK agent path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from open_scientist.tools.registry import ToolContext, tool

logger = logging.getLogger(__name__)


def make_tools(ctx: ToolContext) -> list[Callable[..., Any]]:
    """Return the read_document tool bound to ctx."""

    @tool
    def read_document(file_path: str) -> str:
        """
        Read text content from a PDF, Word, or Excel document.

        Args:
            file_path: Path to the document file (filename or absolute path)

        Returns:
            Extracted text content from the document
        """
        from pathlib import Path as _Path

        from open_scientist.document_reader import read_document as read_doc

        path = _Path(file_path)
        if not path.is_absolute():
            # Resolve by filename in the job data directory
            path = ctx.job_dir / "data" / path.name

        if not path.exists():
            # List available files to help the agent
            data_dir = ctx.job_dir / "data"
            if data_dir.exists():
                available = [f.name for f in data_dir.iterdir() if f.is_file()]
                if available:
                    return (
                        f"❌ File not found: {file_path}\n\n"
                        f"Available files in data directory:\n"
                        + "\n".join(f"  - {name}" for name in available)
                    )
            return f"❌ File not found: {file_path}"

        try:
            return read_doc(path)
        except Exception as e:
            return f"❌ Failed to read document: {e}"

    return [read_document]
