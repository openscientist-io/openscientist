"""
Error parsing and user-friendly message generation for OpenScientist web interface.

Parses raw error messages (API responses, JSON output, etc.) and provides
categorized, actionable error information for display to users.
"""

import re
from typing import Any

# Error category constants
CATEGORY_CONFIGURATION = "configuration"
CATEGORY_PROVIDER = "provider"
CATEGORY_RUNTIME = "runtime"
CATEGORY_RESEARCH = "research"
CATEGORY_UNKNOWN = "unknown"

_TEXT_PATTERN = r'"text"\s*:\s*"([^"]+)"'
_ERROR_PATTERN = r'(?:API Error|Error):\s*(.+?)(?:\.|$|")'
_RESULT_PATTERN = r'"result"\s*:\s*"([^"]+)"'
_COMMON_ERROR_INDICATORS = [
    "does not exist",
    "not found",
    "failed",
    "permission denied",
    "access denied",
    "timeout",
    "connection refused",
]


def _extract_text_field_error(raw_error: str) -> str | None:
    """Extract useful error text from JSON `text` fields."""
    matches = re.findall(_TEXT_PATTERN, raw_error)
    if not matches:
        return None

    for match in matches:
        if "API Error:" in match or "Error:" in match:
            return str(match)

    for match in matches:
        if len(match) > 20 and not match.startswith(("session_id", "uuid", "type")):
            return str(match)

    return None


def _extract_direct_error(raw_error: str) -> str | None:
    """Extract direct API/Error pattern from plain text."""
    error_match = re.search(_ERROR_PATTERN, raw_error, re.IGNORECASE)
    if error_match:
        return error_match.group(0)
    return None


def _extract_result_field_error(raw_error: str) -> str | None:
    """Extract error from JSON `result` field."""
    result_match = re.search(_RESULT_PATTERN, raw_error)
    if result_match:
        return result_match.group(1)
    return None


def _extract_common_indicator_error(raw_error: str) -> str | None:
    """Extract sentence containing common error indicator."""
    lowered = raw_error.lower()
    for indicator in _COMMON_ERROR_INDICATORS:
        if indicator not in lowered:
            continue
        for sentence in raw_error.split("."):
            if indicator in sentence.lower():
                return sentence.strip()
    return None


def _truncate_error(raw_error: str) -> str:
    """Return short user-facing fallback error text."""
    if len(raw_error) <= 200:
        return raw_error

    for line in raw_error.split("\n"):
        if len(line) > 20 and not line.startswith(("{", "[", " ")):
            return line.strip()[:200] + "..."
    return raw_error[:200] + "..."


def extract_error_message(raw_error: str) -> str:
    """
    Extract the actual error message from raw error output.

    API errors often contain JSON blobs with nested content arrays.
    This function extracts the meaningful error text from those structures.

    Args:
        raw_error: Raw error string, potentially containing JSON

    Returns:
        Extracted error message, or original if no pattern matched
    """
    if not raw_error:
        return "Unknown error"

    return (
        _extract_text_field_error(raw_error)
        or _extract_direct_error(raw_error)
        or _extract_result_field_error(raw_error)
        or _extract_common_indicator_error(raw_error)
        or _truncate_error(raw_error)
    )


def categorize_error(error_msg: str) -> str:
    """
    Categorize an error message into one of several types.

    Args:
        error_msg: Error message to categorize

    Returns:
        Category constant (CATEGORY_*)
    """
    error_lower = error_msg.lower()

    # Configuration errors: missing files, paths, permissions
    config_patterns = [
        "does not exist",
        "not found",
        "permission denied",
        "access denied",
        "no such file",
        "invalid path",
        "credentials.json",
        ".env",
        "configuration",
        "not configured",
    ]

    for pattern in config_patterns:
        if pattern in error_lower:
            return CATEGORY_CONFIGURATION

    # Provider errors: API keys, quotas, billing
    provider_patterns = [
        "api key",
        "authentication",
        "quota",
        "budget",
        "billing",
        "rate limit",
        "exceeded",
        "insufficient",
        "unauthorized",
        "forbidden",
    ]

    for pattern in provider_patterns:
        if pattern in error_lower:
            return CATEGORY_PROVIDER

    # Runtime errors: MCP servers, subprocesses, timeouts
    runtime_patterns = [
        "mcp",
        "server",
        "subprocess",
        "timeout",
        "connection",
        "failed to start",
        "crashed",
        "killed",
        "terminated",
    ]

    for pattern in runtime_patterns:
        if pattern in error_lower:
            return CATEGORY_RUNTIME

    # Research errors: data parsing, validation, analysis issues
    research_patterns = [
        "parsing",
        "invalid data",
        "malformed",
        "csv",
        "json",
        "dataframe",
        "column",
        "type error",
    ]

    for pattern in research_patterns:
        if pattern in error_lower:
            return CATEGORY_RESEARCH

    return CATEGORY_UNKNOWN


def get_actionable_steps(category: str, error_msg: str) -> list[str]:
    """
    Get actionable steps based on error category and specific error message.

    Args:
        category: Error category from categorize_error()
        error_msg: The actual error message

    Returns:
        List of actionable step strings
    """
    error_lower = error_msg.lower()

    return _get_steps_for_category(category, error_lower)


def _configuration_steps(error_lower: str) -> list[str]:
    """Get actionable steps for configuration errors."""
    if "gcp-credentials.json" in error_lower or "credentials.json" in error_lower:
        return [
            "Contact your system administrator to configure Google Cloud authentication",
            "Ensure GCP credentials file is mounted at /app/gcp-credentials.json",
            "Check the DEPLOYMENT.md documentation for setup instructions",
        ]
    if ".env" in error_lower:
        return [
            "Check that the .env file exists and contains required configuration",
            "Verify environment variables are properly set",
            "See README.md for required environment variables",
        ]
    if "permission denied" in error_lower:
        return [
            "Check file and directory permissions in the container",
            "Verify the application has write access to the jobs directory",
            "Contact your system administrator if running in restricted environment",
        ]
    return [
        "Verify all required configuration files are present",
        "Check the documentation for setup requirements",
        "Contact your system administrator for assistance",
    ]


def _provider_steps(error_lower: str) -> list[str]:
    """Get actionable steps for cloud provider errors."""
    if "budget" in error_lower or "quota" in error_lower:
        return [
            "Check the billing page to see your current usage",
            "Contact your administrator to increase budget limits",
            "Consider optimizing your research questions to use fewer resources",
        ]
    if "api key" in error_lower or "authentication" in error_lower:
        return [
            "Verify your API credentials are correctly configured",
            "Check that your API key has not expired",
            "Contact your administrator to update credentials",
        ]
    if "rate limit" in error_lower:
        return [
            "Wait a few minutes before retrying the job",
            "Consider reducing max_iterations to lower API usage",
            "Contact your administrator about rate limit increases",
        ]
    return [
        "Check your cloud provider configuration",
        "Verify API credentials and billing status",
        "Contact your system administrator",
    ]


def _runtime_steps(error_lower: str) -> list[str]:
    """Get actionable steps for runtime errors."""
    if "mcp" in error_lower:
        return [
            "This is likely a temporary issue with the MCP server",
            "Try restarting the job",
            "If the problem persists, contact your administrator",
        ]
    if "timeout" in error_lower:
        return [
            "The operation took too long to complete",
            "Try with a simpler research question or fewer iterations",
            "Check your network connection if using remote services",
        ]
    return [
        "This appears to be a runtime issue",
        "Try restarting the job",
        "If the problem persists, contact your administrator with the error details below",
    ]


def _research_steps() -> list[str]:
    """Get actionable steps for research/data errors."""
    return [
        "Check that your uploaded data files are properly formatted",
        "Ensure data files match the expected format (CSV, Excel, etc.)",
        "Try providing a more detailed description of your data in the research question",
        "See the documentation for supported data formats",
    ]


def _unknown_steps() -> list[str]:
    """Get actionable steps for unknown errors."""
    return [
        "An unexpected error occurred",
        "Try restarting the job",
        "If the problem persists, contact your administrator with the error details below",
    ]


def _get_steps_for_category(category: str, error_lower: str) -> list[str]:
    """Dispatch actionable steps generation by category."""
    if category == CATEGORY_CONFIGURATION:
        return _configuration_steps(error_lower)
    if category == CATEGORY_PROVIDER:
        return _provider_steps(error_lower)
    if category == CATEGORY_RUNTIME:
        return _runtime_steps(error_lower)
    if category == CATEGORY_RESEARCH:
        return _research_steps()
    return _unknown_steps()


def get_user_friendly_error(raw_error: str) -> dict[str, Any]:
    """
    Parse a raw error and return user-friendly error information.

    Combines error extraction, categorization, and actionable step generation.

    Args:
        raw_error: Raw error string from job execution

    Returns:
        Dict with keys:
            - category: Error category string
            - title: Short, user-friendly title
            - message: Clear explanation of what went wrong
            - steps: List of actionable steps to resolve
            - raw: Original raw error for technical details
            - contact_admin: Boolean indicating if admin contact is needed
    """
    if not raw_error:
        return {
            "category": CATEGORY_UNKNOWN,
            "title": "Unknown Error",
            "message": "The job failed but no error details were recorded.",
            "steps": ["Contact your administrator with the job ID"],
            "raw": "",
            "contact_admin": True,
        }

    # Extract the actual error message from JSON/CLI output
    error_msg = extract_error_message(raw_error)

    # Categorize the error
    category = categorize_error(error_msg)

    # Generate user-friendly title and message based on category
    if category == CATEGORY_CONFIGURATION:
        title = "Configuration Error"
        message = "OpenScientist could not start due to missing or incorrect configuration. This typically requires administrator intervention to resolve."
    elif category == CATEGORY_PROVIDER:
        title = "Cloud Provider Error"
        message = "There was an issue with your cloud provider (authentication, billing, or quotas). Check the billing page or contact your administrator."
    elif category == CATEGORY_RUNTIME:
        title = "Runtime Error"
        message = "OpenScientist encountered an error during execution. This may be temporary - try running the job again."
    elif category == CATEGORY_RESEARCH:
        title = "Data Processing Error"
        message = "OpenScientist had trouble processing your data files. Check that files are properly formatted and match the expected structure."
    else:
        title = "Unexpected Error"
        message = "An unexpected error occurred during job execution."

    # Get actionable steps
    steps = get_actionable_steps(category, error_msg)

    # Determine if admin contact is needed
    contact_admin = category in [
        CATEGORY_CONFIGURATION,
        CATEGORY_PROVIDER,
        CATEGORY_UNKNOWN,
    ]

    return {
        "category": category,
        "title": title,
        "message": message,
        "extracted_error": error_msg,
        "steps": steps,
        "raw": raw_error,
        "contact_admin": contact_admin,
    }
