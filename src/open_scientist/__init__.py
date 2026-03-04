"""
Open Scientist - Scientific Hypothesis Agent for Novel Discovery

An autonomous AI scientist that generates and tests hypotheses from scientific data.
"""

from open_scientist.exceptions import (
    BudgetExceededError,
    CodeExecutionError,
    CodeExecutionTimeoutError,
    FileLoadError,
    FileTooBigError,
    ForbiddenImportError,
    OpenScientistError,
    PDFGenerationError,
    ProviderError,
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
    "OpenScientistError",
    "UnsupportedFileTypeError",
]
