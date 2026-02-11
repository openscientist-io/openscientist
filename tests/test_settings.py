"""Tests for centralized settings module."""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from shandy.settings import (
    AuthSettings,
    BudgetSettings,
    ContainerSettings,
    DatabaseSettings,
    FileSettings,
    PhenixSettings,
    ProviderSettings,
    clear_settings_cache,
    get_settings,
)


class TestProviderSettings:
    """Tests for provider configuration validation."""

    def test_anthropic_requires_api_key(self):
        """Anthropic provider requires ANTHROPIC_API_KEY."""
        with pytest.raises(ValidationError) as exc_info:
            ProviderSettings(
                CLAUDE_PROVIDER="anthropic",
                ANTHROPIC_API_KEY=None,
            )
        assert "ANTHROPIC_API_KEY" in str(exc_info.value)

    def test_anthropic_valid_config(self):
        """Valid Anthropic configuration passes validation."""
        settings = ProviderSettings(
            CLAUDE_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="sk-ant-test-key",
        )
        assert settings.claude_provider == "anthropic"
        assert settings.anthropic_api_key == "sk-ant-test-key"

    def test_cborg_requires_auth_token(self):
        """CBORG provider requires ANTHROPIC_AUTH_TOKEN."""
        with pytest.raises(ValidationError) as exc_info:
            ProviderSettings(
                CLAUDE_PROVIDER="cborg",
                ANTHROPIC_AUTH_TOKEN=None,
            )
        assert "ANTHROPIC_AUTH_TOKEN" in str(exc_info.value)

    def test_cborg_requires_base_url(self):
        """CBORG provider requires ANTHROPIC_BASE_URL."""
        with pytest.raises(ValidationError) as exc_info:
            ProviderSettings(
                CLAUDE_PROVIDER="cborg",
                ANTHROPIC_AUTH_TOKEN="test-token",
                ANTHROPIC_BASE_URL=None,
            )
        assert "ANTHROPIC_BASE_URL" in str(exc_info.value)

    def test_cborg_valid_config(self):
        """Valid CBORG configuration passes validation."""
        settings = ProviderSettings(
            CLAUDE_PROVIDER="cborg",
            ANTHROPIC_AUTH_TOKEN="test-token",
            ANTHROPIC_BASE_URL="https://api.cborg.lbl.gov",
        )
        assert settings.claude_provider == "cborg"

    def test_vertex_requires_project_id(self):
        """Vertex AI provider requires project ID."""
        with patch("os.path.exists", return_value=True):
            with pytest.raises(ValidationError) as exc_info:
                ProviderSettings(
                    CLAUDE_PROVIDER="vertex",
                    ANTHROPIC_VERTEX_PROJECT_ID=None,
                    GOOGLE_APPLICATION_CREDENTIALS="/path/to/creds.json",
                    GCP_BILLING_ACCOUNT_ID="123-456-789",
                    CLOUD_ML_REGION="us-east5",
                )
        assert "ANTHROPIC_VERTEX_PROJECT_ID" in str(exc_info.value)

    def test_vertex_requires_credentials_file(self):
        """Vertex AI provider requires credentials file to exist."""
        with patch("os.path.exists", return_value=False):
            with pytest.raises(ValidationError) as exc_info:
                ProviderSettings(
                    CLAUDE_PROVIDER="vertex",
                    ANTHROPIC_VERTEX_PROJECT_ID="my-project",
                    GOOGLE_APPLICATION_CREDENTIALS="/nonexistent/creds.json",
                    GCP_BILLING_ACCOUNT_ID="123-456-789",
                    CLOUD_ML_REGION="us-east5",
                )
        assert "not found" in str(exc_info.value)

    def test_bedrock_requires_region(self):
        """Bedrock provider requires AWS_REGION."""
        with pytest.raises(ValidationError) as exc_info:
            ProviderSettings(
                CLAUDE_PROVIDER="bedrock",
                AWS_REGION=None,
                AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
                AWS_SECRET_ACCESS_KEY="secret",
            )
        assert "AWS_REGION" in str(exc_info.value)

    def test_bedrock_requires_credentials(self):
        """Bedrock provider requires at least one credential method."""
        with pytest.raises(ValidationError) as exc_info:
            ProviderSettings(
                CLAUDE_PROVIDER="bedrock",
                AWS_REGION="us-east-1",
                AWS_ACCESS_KEY_ID=None,
                AWS_SECRET_ACCESS_KEY=None,
                AWS_PROFILE=None,
                AWS_BEARER_TOKEN_BEDROCK=None,
            )
        assert "credentials" in str(exc_info.value).lower()

    def test_bedrock_valid_with_access_key(self):
        """Bedrock with access key/secret is valid."""
        settings = ProviderSettings(
            CLAUDE_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
            AWS_SECRET_ACCESS_KEY="secret",
        )
        assert settings.claude_provider == "bedrock"

    def test_bedrock_valid_with_profile(self):
        """Bedrock with profile is valid."""
        settings = ProviderSettings(
            CLAUDE_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_PROFILE="default",
        )
        assert settings.claude_provider == "bedrock"

    def test_unknown_provider_rejected(self):
        """Unknown provider is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ProviderSettings(CLAUDE_PROVIDER="unknown-provider")
        assert "Unknown provider" in str(exc_info.value)

    def test_codex_minimal_config(self):
        """Codex provider has minimal requirements."""
        settings = ProviderSettings(CLAUDE_PROVIDER="codex")
        assert settings.claude_provider == "codex"


class TestDatabaseSettings:
    """Tests for database configuration."""

    def test_database_url_used_directly(self):
        """DATABASE_URL is used directly if provided."""
        settings = DatabaseSettings(DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db")
        assert settings.effective_database_url == "postgresql+asyncpg://user:pass@host:5432/db"

    def test_database_url_constructed_from_components(self):
        """Database URL is constructed from components when DATABASE_URL not set."""
        settings = DatabaseSettings(
            DATABASE_URL=None,
            POSTGRES_HOST="localhost",
            POSTGRES_PORT=5432,
            POSTGRES_USER="shandy",
            POSTGRES_PASSWORD="secret",
            POSTGRES_DB="shandy_db",
        )
        expected = "postgresql+asyncpg://shandy:secret@localhost:5432/shandy_db"
        assert settings.effective_database_url == expected

    def test_database_requires_password_or_url(self):
        """Either DATABASE_URL or POSTGRES_PASSWORD must be set."""
        settings = DatabaseSettings(
            DATABASE_URL=None,
            POSTGRES_PASSWORD="",
        )
        with pytest.raises(ValueError) as exc_info:
            _ = settings.effective_database_url
        assert "DATABASE_URL" in str(exc_info.value)

    def test_sql_echo_default_false(self):
        """SQL_ECHO defaults to False."""
        settings = DatabaseSettings()
        assert settings.sql_echo is False


class TestAuthSettings:
    """Tests for authentication configuration."""

    def test_github_oauth_requires_both_id_and_secret(self):
        """GitHub OAuth requires both client ID and secret."""
        with pytest.raises(ValidationError) as exc_info:
            AuthSettings(
                GITHUB_CLIENT_ID="test-id",
                GITHUB_CLIENT_SECRET=None,
            )
        assert "GITHUB_CLIENT_SECRET" in str(exc_info.value)

    def test_github_oauth_requires_id_if_secret_set(self):
        """GitHub OAuth requires ID if secret is set."""
        with pytest.raises(ValidationError) as exc_info:
            AuthSettings(
                GITHUB_CLIENT_ID=None,
                GITHUB_CLIENT_SECRET="test-secret",
            )
        assert "GITHUB_CLIENT_ID" in str(exc_info.value)

    def test_orcid_oauth_requires_both_id_and_secret(self):
        """ORCID OAuth requires both client ID and secret."""
        with pytest.raises(ValidationError) as exc_info:
            AuthSettings(
                ORCID_CLIENT_ID="test-id",
                ORCID_CLIENT_SECRET=None,
            )
        assert "ORCID_CLIENT_SECRET" in str(exc_info.value)

    def test_valid_github_oauth(self):
        """Valid GitHub OAuth configuration passes."""
        settings = AuthSettings(
            GITHUB_CLIENT_ID="test-id",
            GITHUB_CLIENT_SECRET="test-secret",
        )
        assert settings.github_client_id == "test-id"
        assert settings.is_oauth_configured is True

    def test_is_oauth_configured_with_mock_auth(self):
        """Mock auth counts as OAuth configured."""
        settings = AuthSettings(ENABLE_MOCK_AUTH=True)
        assert settings.is_oauth_configured is True

    def test_is_oauth_configured_false_when_none_set(self):
        """is_oauth_configured is False when nothing is configured."""
        settings = AuthSettings()
        assert settings.is_oauth_configured is False


class TestBudgetSettings:
    """Tests for budget configuration."""

    def test_positive_budget_values_required(self):
        """Budget values must be positive."""
        with pytest.raises(ValidationError) as exc_info:
            BudgetSettings(MAX_JOB_COST_USD=-10.0)
        assert "must be positive" in str(exc_info.value)

    def test_zero_budget_rejected(self):
        """Zero budget is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            BudgetSettings(APP_MAX_BUDGET_USD=0)
        assert "must be positive" in str(exc_info.value)

    def test_valid_budget_values(self):
        """Valid budget values pass validation."""
        settings = BudgetSettings(
            MAX_PROJECT_SPEND_WARN=50.0,
            MAX_PROJECT_SPEND_HARD=200.0,
            MAX_JOB_COST_USD=5.0,
            APP_MAX_BUDGET_USD=500.0,
        )
        assert settings.max_job_cost_usd == 5.0
        assert settings.app_max_budget_usd == 500.0


class TestPhenixSettings:
    """Tests for Phenix configuration validation."""

    def test_phenix_path_absolute_required(self):
        """PHENIX_PATH must be an absolute path."""
        with pytest.raises(ValidationError) as exc_info:
            PhenixSettings(PHENIX_PATH="relative/path")
        assert "absolute path" in str(exc_info.value)

    def test_phenix_path_no_traversal(self):
        """PHENIX_PATH must not contain path traversal."""
        with patch("os.path.exists", return_value=True):
            with patch("os.path.isdir", return_value=True):
                with pytest.raises(ValidationError) as exc_info:
                    PhenixSettings(PHENIX_PATH="/opt/../etc/phenix")
        assert "path traversal" in str(exc_info.value)

    def test_phenix_nonexistent_path_accepted(self):
        """PHENIX_PATH with valid format but nonexistent path is accepted (existence checked by is_available)."""
        # Nonexistent path is accepted at validation time
        settings = PhenixSettings(PHENIX_PATH="/nonexistent/phenix/path")
        assert settings.phenix_path == "/nonexistent/phenix/path"
        # But is_available returns False
        assert settings.is_available is False

    def test_phenix_file_instead_of_directory(self):
        """PHENIX_PATH pointing to a file (not directory) results in is_available=False."""
        with patch("os.path.isdir", return_value=False):
            settings = PhenixSettings(PHENIX_PATH="/some/file.txt")
            # Format is valid, so accepted
            assert settings.phenix_path == "/some/file.txt"
            # But is_available returns False because it's not a directory
            assert settings.is_available is False

    def test_phenix_path_none_is_valid(self):
        """None PHENIX_PATH is valid (Phenix is optional)."""
        settings = PhenixSettings(PHENIX_PATH=None)
        assert settings.phenix_path is None
        assert settings.is_available is False

    def test_phenix_is_available_checks_env_script(self):
        """is_available checks for phenix_env.sh."""
        with patch("os.path.exists") as mock_exists:
            with patch("os.path.isdir", return_value=True):
                # Directory exists and phenix_env.sh exists
                mock_exists.side_effect = lambda p: True
                settings = PhenixSettings(PHENIX_PATH="/opt/phenix")
                assert settings.is_available is True

    def test_phenix_not_available_without_env_script(self):
        """is_available is False when phenix_env.sh is missing."""
        with patch("os.path.exists") as mock_exists:
            with patch("os.path.isdir", return_value=True):
                # Directory exists but phenix_env.sh does not
                def exists_side_effect(path):
                    return not path.endswith("phenix_env.sh")

                mock_exists.side_effect = exists_side_effect
                settings = PhenixSettings(PHENIX_PATH="/opt/phenix")
                assert settings.is_available is False


class TestFileSettings:
    """Tests for file settings."""

    def test_max_file_size_must_be_positive(self):
        """MAX_FILE_SIZE_MB must be positive."""
        with pytest.raises(ValidationError) as exc_info:
            FileSettings(MAX_FILE_SIZE_MB=-100)
        assert "must be positive" in str(exc_info.value)

    def test_default_file_size(self):
        """Default file size is 1000 MB."""
        settings = FileSettings()
        assert settings.max_file_size_mb == 1000


class TestContainerSettings:
    """Tests for container settings."""

    def test_default_values(self):
        """Default container settings are reasonable."""
        settings = ContainerSettings()
        assert settings.use_container_isolation is False
        assert settings.executor_image == "shandy-executor:latest"
        assert settings.executor_memory == "2g"
        assert settings.executor_cpu == 0.5
        assert settings.executor_timeout == 120

    def test_custom_values(self):
        """Custom container settings are applied."""
        settings = ContainerSettings(
            SHANDY_USE_CONTAINER_ISOLATION=True,
            SHANDY_EXECUTOR_IMAGE="custom-executor:v1",
            SHANDY_EXECUTOR_MEMORY="4g",
            SHANDY_EXECUTOR_CPU=1.0,
            SHANDY_EXECUTOR_TIMEOUT=300,
        )
        assert settings.use_container_isolation is True
        assert settings.executor_image == "custom-executor:v1"
        assert settings.executor_memory == "4g"
        assert settings.executor_cpu == 1.0
        assert settings.executor_timeout == 300


class TestGetSettings:
    """Tests for settings singleton."""

    def setup_method(self):
        """Clear settings cache before each test."""
        clear_settings_cache()

    def teardown_method(self):
        """Clear settings cache after each test."""
        clear_settings_cache()

    def test_get_settings_returns_singleton(self, monkeypatch, tmp_path):
        """get_settings returns the same instance on multiple calls."""
        # Change to temp directory without .env file to avoid picking up project .env
        monkeypatch.chdir(tmp_path)

        # Clear any env vars that might cause validation errors
        monkeypatch.delenv("PHENIX_PATH", raising=False)

        # Set minimal valid config for anthropic provider
        monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_clear_cache_allows_reload(self, monkeypatch, tmp_path):
        """clear_settings_cache allows settings to be reloaded."""
        # Change to temp directory without .env file to avoid picking up project .env
        monkeypatch.chdir(tmp_path)

        # Clear any env vars that might cause validation errors
        monkeypatch.delenv("PHENIX_PATH", raising=False)

        # Set minimal valid config for anthropic provider
        monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        settings1 = get_settings()
        clear_settings_cache()
        settings2 = get_settings()
        # Different instances (but same values)
        assert settings1 is not settings2
