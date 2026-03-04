"""
SHANDY - Scientific Hypothesis Agent for Novel Discovery

An autonomous AI scientist that generates and tests hypotheses from scientific data.
"""

from shandy.exceptions import (
    BudgetExceededError,
    CodeExecutionError,
    CodeExecutionTimeoutError,
    FileLoadError,
    FileTooBigError,
    ForbiddenImportError,
    PDFGenerationError,
    ProviderError,
    ShandyError,
    UnsupportedFileTypeError,
)

__all__ = [
    "BudgetExceededError",
    "CodeExecutionError",
    "CodeExecutionTimeoutError",
    "FileLoadError",
    "FileTooBigError",
    "ForbiddenImportError",
    "PDFGenerationError",
    "ProviderError",
    "ShandyError",
    "UnsupportedFileTypeError",
]
