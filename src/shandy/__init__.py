"""
SHANDY - Scientific Hypothesis Agent for Novel Discovery

An autonomous AI scientist that generates and tests hypotheses from scientific data.
"""

from shandy.exceptions import (  # noqa: F401
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
