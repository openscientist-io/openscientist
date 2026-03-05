"""Tests for error handler module."""

from openscientist.webapp_components.error_handler import (
    CATEGORY_CONFIGURATION,
    CATEGORY_PROVIDER,
    CATEGORY_RESEARCH,
    CATEGORY_RUNTIME,
    CATEGORY_UNKNOWN,
    categorize_error,
    extract_error_message,
    get_actionable_steps,
    get_user_friendly_error,
)


class TestExtractErrorMessage:
    """Tests for extract_error_message function."""

    def test_empty_error(self):
        """Test handling of empty error."""
        assert extract_error_message("") == "Unknown error"

    def test_simple_error(self):
        """Test simple error message."""
        error = "Simple error message"
        assert extract_error_message(error) == "Simple error message"

    def test_api_error_in_json(self):
        """Test extraction of API Error from JSON."""
        error = '{"text": "API Error: Invalid credentials", "type": "error"}'
        result = extract_error_message(error)
        assert "API Error: Invalid credentials" in result

    def test_nested_json_text_field(self):
        """Test extraction from nested JSON text field."""
        error = '{"content": [{"text": "Error: File not found"}]}'
        result = extract_error_message(error)
        assert "Error: File not found" in result

    def test_result_field_extraction(self):
        """Test extraction from result field."""
        error = '{"result": "Operation failed with error"}'
        result = extract_error_message(error)
        assert "Operation failed with error" in result

    def test_common_error_patterns(self):
        """Test detection of common error patterns."""
        errors = [
            "The file does not exist in the system",
            "Resource not found in database",
            "Operation failed unexpectedly",
            "Permission denied for user",
            "Access denied to resource",
            "Connection timeout occurred",
            "Connection refused by server",
        ]
        for error in errors:
            result = extract_error_message(error)
            assert len(result) > 0
            assert result != "Unknown error"

    def test_long_error_truncation(self):
        """Test that very long errors are truncated."""
        error = "x" * 500  # Very long error
        result = extract_error_message(error)
        assert len(result) <= 203  # 200 + "..."

    def test_short_error_passthrough(self):
        """Test that short errors pass through unchanged."""
        error = "Short error"
        result = extract_error_message(error)
        assert result == "Short error"

    def test_multiline_error_extraction(self):
        """Test extraction from multiline errors."""
        error = "Line 1\nLine 2 contains the actual error message\nLine 3"
        result = extract_error_message(error)
        assert isinstance(result, str)
        assert len(result) > 0


class TestCategorizeError:
    """Tests for categorize_error function."""

    def test_configuration_errors(self):
        """Test categorization of configuration errors."""
        errors = [
            "File does not exist",
            "Path not found in system",
            "Permission denied to access file",
            "No such file or directory",
            "Invalid path specified",
            "credentials.json missing",
            ".env file not configured",
            "Configuration error occurred",
        ]
        for error in errors:
            assert categorize_error(error) == CATEGORY_CONFIGURATION

    def test_provider_errors(self):
        """Test categorization of provider errors."""
        errors = [
            "API key is invalid",
            "Authentication failed",
            "Quota exceeded for this month",
            "Budget limit reached",
            "Billing error occurred",
            "Rate limit exceeded",
            "Insufficient permissions",
            "Unauthorized access",
            "Forbidden resource",
        ]
        for error in errors:
            assert categorize_error(error) == CATEGORY_PROVIDER

    def test_runtime_errors(self):
        """Test categorization of runtime errors."""
        errors = [
            "MCP server connection failed",
            "Server is not responding",
            "Subprocess crashed",
            "Operation timeout",
            "Connection error",
            "Failed to start process",
            "Process was killed",
            "Service terminated unexpectedly",
        ]
        for error in errors:
            assert categorize_error(error) == CATEGORY_RUNTIME

    def test_research_errors(self):
        """Test categorization of research/data errors."""
        errors = [
            "Error parsing CSV file",
            "Malformed JSON structure",
            "CSV parsing failed",
            "DataFrame creation failed",
            "Type error in data processing",
            "Column mismatch in dataframe",
        ]
        for error in errors:
            assert categorize_error(error) == CATEGORY_RESEARCH

    def test_unknown_error(self):
        """Test that unrecognized errors are categorized as unknown."""
        error = "Some completely random error that doesn't match any pattern"
        assert categorize_error(error) == CATEGORY_UNKNOWN


class TestGetActionableSteps:
    """Tests for get_actionable_steps function."""

    def test_gcp_credentials_error(self):
        """Test actionable steps for GCP credentials error."""
        error = "gcp-credentials.json not found"
        steps = get_actionable_steps(CATEGORY_CONFIGURATION, error)
        assert len(steps) > 0
        assert any("gcp" in step.lower() or "credentials" in step.lower() for step in steps)

    def test_env_file_error(self):
        """Test actionable steps for .env file error."""
        error = ".env file missing"
        steps = get_actionable_steps(CATEGORY_CONFIGURATION, error)
        assert len(steps) > 0
        assert any(".env" in step for step in steps)

    def test_permission_denied_error(self):
        """Test actionable steps for permission error."""
        error = "Permission denied"
        steps = get_actionable_steps(CATEGORY_CONFIGURATION, error)
        assert len(steps) > 0
        assert any("permission" in step.lower() for step in steps)

    def test_budget_error(self):
        """Test actionable steps for budget error."""
        error = "Budget quota exceeded"
        steps = get_actionable_steps(CATEGORY_PROVIDER, error)
        assert len(steps) > 0
        assert any("budget" in step.lower() for step in steps)

    def test_api_key_error(self):
        """Test actionable steps for API key error."""
        error = "API key invalid"
        steps = get_actionable_steps(CATEGORY_PROVIDER, error)
        assert len(steps) > 0
        assert any("api" in step.lower() or "credentials" in step.lower() for step in steps)

    def test_rate_limit_error(self):
        """Test actionable steps for rate limit error."""
        error = "Rate limit exceeded"
        steps = get_actionable_steps(CATEGORY_PROVIDER, error)
        assert len(steps) > 0
        assert any("rate limit" in step.lower() or "wait" in step.lower() for step in steps)

    def test_mcp_error(self):
        """Test actionable steps for MCP error."""
        error = "MCP server failed to start"
        steps = get_actionable_steps(CATEGORY_RUNTIME, error)
        assert len(steps) > 0
        assert any("mcp" in step.lower() for step in steps)

    def test_timeout_error(self):
        """Test actionable steps for timeout error."""
        error = "Operation timeout"
        steps = get_actionable_steps(CATEGORY_RUNTIME, error)
        assert len(steps) > 0
        assert any("timeout" in step.lower() or "took too long" in step.lower() for step in steps)

    def test_research_error(self):
        """Test actionable steps for research/data error."""
        error = "Data parsing failed"
        steps = get_actionable_steps(CATEGORY_RESEARCH, error)
        assert len(steps) > 0
        assert any("data" in step.lower() or "format" in step.lower() for step in steps)

    def test_unknown_error(self):
        """Test actionable steps for unknown error."""
        error = "Unknown problem"
        steps = get_actionable_steps(CATEGORY_UNKNOWN, error)
        assert len(steps) > 0
        assert any(
            "unexpected" in step.lower() or "administrator" in step.lower() for step in steps
        )


class TestGetUserFriendlyError:
    """Tests for get_user_friendly_error function."""

    def test_empty_error_handling(self):
        """Test handling of empty error."""
        result = get_user_friendly_error("")
        assert result["category"] == CATEGORY_UNKNOWN
        assert result["title"] == "Unknown Error"
        assert result["contact_admin"] is True

    def test_configuration_error_workflow(self):
        """Test complete workflow for configuration error."""
        raw_error = '{"text": "API Error: credentials.json file not found"}'
        result = get_user_friendly_error(raw_error)

        assert result["category"] == CATEGORY_CONFIGURATION
        assert result["title"] == "Configuration Error"
        assert "configuration" in result["message"].lower()
        assert len(result["steps"]) > 0
        assert result["contact_admin"] is True
        assert "credentials.json" in result["extracted_error"]

    def test_provider_error_workflow(self):
        """Test complete workflow for provider error."""
        raw_error = "Authentication failed: API key is invalid"
        result = get_user_friendly_error(raw_error)

        assert result["category"] == CATEGORY_PROVIDER
        assert result["title"] == "Cloud Provider Error"
        assert "provider" in result["message"].lower()
        assert len(result["steps"]) > 0
        assert result["contact_admin"] is True

    def test_runtime_error_workflow(self):
        """Test complete workflow for runtime error."""
        raw_error = "MCP server connection timeout"
        result = get_user_friendly_error(raw_error)

        assert result["category"] == CATEGORY_RUNTIME
        assert result["title"] == "Runtime Error"
        assert "execution" in result["message"].lower() or "runtime" in result["message"].lower()
        assert len(result["steps"]) > 0
        assert result["contact_admin"] is False

    def test_research_error_workflow(self):
        """Test complete workflow for research error."""
        raw_error = "CSV parsing failed: malformed data"
        result = get_user_friendly_error(raw_error)

        assert result["category"] == CATEGORY_RESEARCH
        assert result["title"] == "Data Processing Error"
        assert "data" in result["message"].lower()
        assert len(result["steps"]) > 0
        assert result["contact_admin"] is False

    def test_unknown_error_workflow(self):
        """Test complete workflow for unknown error."""
        raw_error = "Something completely unexpected happened"
        result = get_user_friendly_error(raw_error)

        assert result["category"] == CATEGORY_UNKNOWN
        assert result["title"] == "Unexpected Error"
        assert len(result["steps"]) > 0
        assert result["contact_admin"] is True

    def test_result_structure(self):
        """Test that result has all expected keys."""
        result = get_user_friendly_error("test error")

        required_keys = [
            "category",
            "title",
            "message",
            "extracted_error",
            "steps",
            "raw",
            "contact_admin",
        ]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

        assert isinstance(result["steps"], list)
        assert isinstance(result["contact_admin"], bool)
        assert result["raw"] == "test error"

    def test_complex_nested_json_error(self):
        """Test handling of complex nested JSON error."""
        raw_error = """
        {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": "Rate limit exceeded"
            }
        }
        """
        result = get_user_friendly_error(raw_error)

        assert result["category"] == CATEGORY_PROVIDER
        assert len(result["steps"]) > 0
        assert result["raw"] == raw_error
