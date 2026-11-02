"""
Structured exception hierarchy for OpenScientist.
"""


class OpenScientistError(Exception):
    """Base exception for all OpenScientist errors."""


# ── Code execution ────────────────────────────────────────────────────


class CodeExecutionError(OpenScientistError):
    """Raised when user-submitted code execution fails."""


class CodeExecutionTimeoutError(CodeExecutionError):
    """Raised when code execution exceeds its time limit."""


class ForbiddenImportError(CodeExecutionError):
    """Raised when code tries to import a disallowed module."""


# ── File loading / upload ─────────────────────────────────────────────


class FileLoadError(OpenScientistError):
    """Raised when a data file cannot be loaded or parsed."""


class FileTooBigError(FileLoadError):
    """Raised when a file exceeds the size limit."""


class UnsupportedFileTypeError(FileLoadError):
    """Raised when a file type is not supported."""


# ── Budget / cost tracking ────────────────────────────────────────────


class BudgetExceededError(OpenScientistError, ValueError):
    """Raised when a budget limit refuses a job's creation, start, chat turn or
    report regeneration. A ValueError, so the API returns it like any other
    create-job refusal."""


# ── Provider errors ───────────────────────────────────────────────────


class ProviderError(OpenScientistError):
    """Raised when a model-provider operation (API call, billing query) fails."""


# ── PDF generation ────────────────────────────────────────────────────


class PDFGenerationError(OpenScientistError):
    """Raised when PDF report generation fails."""


class McpToolsUnavailableError(OpenScientistError):
    """Raised when a turn ran without the openscientist-tools MCP tools."""


# ── Containers ────────────────────────────────────────────────────────

#: The command that builds every image the web runs.
BUILD_IMAGES_COMMAND = "uv run openscientist"


class MissingImageError(OpenScientistError):
    """Raised when a container image the web runs is not on the Docker host."""

    def __init__(self, image: str) -> None:
        super().__init__(
            f"Container image {image} not found. Build the images with '{BUILD_IMAGES_COMMAND}'."
        )
        self.image = image
