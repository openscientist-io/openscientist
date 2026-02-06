"""
SHANDY - Scientific Hypothesis Agent for Novel Discovery

An autonomous AI scientist that generates and tests hypotheses from scientific data.
"""

__version__ = "0.1.0"

from shandy.exceptions import (  # noqa: F401
    BudgetExceededError,
    CodeExecutionError,
    CodeExecutionTimeoutError,
    FileLoadError,
    FileTooBigError,
    ForbiddenImportError,
    JobError,
    PDFGenerationError,
    ProviderError,
    ShandyError,
    UnsupportedFileTypeError,
)
