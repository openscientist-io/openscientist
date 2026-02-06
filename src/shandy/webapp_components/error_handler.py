"""
Error parsing and user-friendly message generation for SHANDY web interface.

Parses raw error messages (especially Claude CLI JSON output) and provides
categorized, actionable error information for display to users.
"""

import re
from typing import Dict, List

# Error category constants
CATEGORY_CONFIGURATION = "configuration"
CATEGORY_PROVIDER = "provider"
CATEGORY_RUNTIME = "runtime"
CATEGORY_RESEARCH = "research"
CATEGORY_UNKNOWN = "unknown"


def extract_error_message(raw_error: str) -> str:
    """
    Extract the actual error message from Claude CLI JSON output.

    Claude CLI errors often contain JSON blobs with nested content arrays.
    This function extracts the meaningful error text from those structures.

    Args:
        raw_error: Raw error string, potentially containing JSON

    Returns:
        Extracted error message, or original if no pattern matched
    """
    if not raw_error:
        return "Unknown error"

    # Pattern 1: Look for "text": "API Error: ..." in JSON content
    # This handles the nested JSON structure from Claude CLI
    text_pattern = r'"text"\s*:\s*"([^"]+)"'
    matches = re.findall(text_pattern, raw_error)

    for match in matches:
        # Prioritize "API Error:" messages
        if "API Error:" in match or "Error:" in match:
            return str(match)

    # If we found any text fields but no explicit errors, use the first substantive one
    if matches:
        for match in matches:
            # Skip system messages, focus on actual errors
            if len(match) > 20 and not match.startswith(("session_id", "uuid", "type")):
                return str(match)

    # Pattern 2: Direct "API Error:" or "Error:" in the string
    error_pattern = r'(?:API Error|Error):\s*(.+?)(?:\.|$|")'
    error_match = re.search(error_pattern, raw_error, re.IGNORECASE)
    if error_match:
        return error_match.group(0)

    # Pattern 3: Extract from "result": "..." field
    result_pattern = r'"result"\s*:\s*"([^"]+)"'
    result_match = re.search(result_pattern, raw_error)
    if result_match:
        return result_match.group(1)

    # Pattern 4: Look for common error indicators
    common_errors = [
        "does not exist",
        "not found",
        "failed",
        "permission denied",
        "access denied",
        "timeout",
        "connection refused",
    ]

    for error_indicator in common_errors:
        if error_indicator.lower() in raw_error.lower():
            # Find the sentence containing this error
            sentences = raw_error.split(".")
            for sentence in sentences:
                if error_indicator.lower() in sentence.lower():
                    return sentence.strip()

    # Fallback: If error is short, return as-is; if long, truncate intelligently
    if len(raw_error) <= 200:
        return raw_error

    # Try to get first meaningful line
    lines = raw_error.split("\n")
    for line in lines:
        if len(line) > 20 and not line.startswith(("{", "[", " ")):
            return line.strip()[:200] + "..."

    # Ultimate fallback
    return raw_error[:200] + "..."


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


def get_actionable_steps(category: str, error_msg: str) -> List[str]:
    """
    Get actionable steps based on error category and specific error message.

    Args:
        category: Error category from categorize_error()
        error_msg: The actual error message

    Returns:
        List of actionable step strings
    """
    error_lower = error_msg.lower()

    if category == CATEGORY_CONFIGURATION:
        if "gcp-credentials.json" in error_lower or "credentials.json" in error_lower:
            return [
                "Contact your system administrator to configure Google Cloud authentication",
                "Ensure GCP credentials file is mounted at /app/gcp-credentials.json",
                "Check the DEPLOYMENT.md documentation for setup instructions",
            ]
        elif ".env" in error_lower:
            return [
                "Check that the .env file exists and contains required configuration",
                "Verify environment variables are properly set",
                "See README.md for required environment variables",
            ]
        elif "permission denied" in error_lower:
            return [
                "Check file and directory permissions in the container",
                "Verify the application has write access to the jobs directory",
                "Contact your system administrator if running in restricted environment",
            ]
        else:
            return [
                "Verify all required configuration files are present",
                "Check the documentation for setup requirements",
                "Contact your system administrator for assistance",
            ]

    elif category == CATEGORY_PROVIDER:
        if "budget" in error_lower or "quota" in error_lower:
            return [
                "Check the billing page to see your current usage",
                "Contact your administrator to increase budget limits",
                "Consider optimizing your research questions to use fewer resources",
            ]
        elif "api key" in error_lower or "authentication" in error_lower:
            return [
                "Verify your API credentials are correctly configured",
                "Check that your API key has not expired",
                "Contact your administrator to update credentials",
            ]
        elif "rate limit" in error_lower:
            return [
                "Wait a few minutes before retrying the job",
                "Consider reducing max_iterations to lower API usage",
                "Contact your administrator about rate limit increases",
            ]
        else:
            return [
                "Check your cloud provider configuration",
                "Verify API credentials and billing status",
                "Contact your system administrator",
            ]

    elif category == CATEGORY_RUNTIME:
        if "mcp" in error_lower:
            return [
                "This is likely a temporary issue with the MCP server",
                "Try restarting the job",
                "If the problem persists, contact your administrator",
            ]
        elif "timeout" in error_lower:
            return [
                "The operation took too long to complete",
                "Try with a simpler research question or fewer iterations",
                "Check your network connection if using remote services",
            ]
        else:
            return [
                "This appears to be a runtime issue",
                "Try restarting the job",
                "If the problem persists, contact your administrator with the error details below",
            ]

    elif category == CATEGORY_RESEARCH:
        return [
            "Check that your uploaded data files are properly formatted",
            "Ensure data files match the expected format (CSV, Excel, etc.)",
            "Try providing a more detailed description of your data in the research question",
            "See the documentation for supported data formats",
        ]

    else:  # CATEGORY_UNKNOWN
        return [
            "An unexpected error occurred",
            "Try restarting the job",
            "If the problem persists, contact your administrator with the error details below",
        ]


def get_user_friendly_error(raw_error: str) -> Dict:
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
        message = "SHANDY could not start due to missing or incorrect configuration. This typically requires administrator intervention to resolve."
    elif category == CATEGORY_PROVIDER:
        title = "Cloud Provider Error"
        message = "There was an issue with your cloud provider (authentication, billing, or quotas). Check the billing page or contact your administrator."
    elif category == CATEGORY_RUNTIME:
        title = "Runtime Error"
        message = "SHANDY encountered an error during execution. This may be temporary - try running the job again."
    elif category == CATEGORY_RESEARCH:
        title = "Data Processing Error"
        message = "SHANDY had trouble processing your data files. Check that files are properly formatted and match the expected structure."
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
