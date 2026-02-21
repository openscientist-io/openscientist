"""
Structured exception hierarchy for SHANDY.
"""


class ShandyError(Exception):
    """Base exception for all SHANDY errors."""


# ── Code execution ────────────────────────────────────────────────────


class CodeExecutionError(ShandyError):
    """Raised when user-submitted code execution fails."""


class CodeExecutionTimeoutError(CodeExecutionError):
    """Raised when code execution exceeds its time limit."""


class ForbiddenImportError(CodeExecutionError):
    """Raised when code tries to import a disallowed module."""


# ── File loading / upload ─────────────────────────────────────────────


class FileLoadError(ShandyError):
    """Raised when a data file cannot be loaded or parsed."""


class FileTooBigError(FileLoadError):
    """Raised when a file exceeds the size limit."""


class UnsupportedFileTypeError(FileLoadError):
    """Raised when a file type is not supported."""


# ── Budget / cost tracking ────────────────────────────────────────────


class BudgetExceededError(ShandyError):
    """Raised when a job exceeds its budget limit."""


# ── Provider errors ───────────────────────────────────────────────────


class ProviderError(ShandyError):
    """Raised when a model-provider operation (API call, billing query) fails."""


# ── PDF generation ────────────────────────────────────────────────────


class PDFGenerationError(ShandyError):
    """Raised when PDF report generation fails."""
