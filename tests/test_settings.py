"""Tests for centralized settings module."""

import logging
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from open_scientist.settings import (
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
    """Tests for provider configuration validation.

    The provider validator is warn-only — missing credentials log a warning
    but do not prevent the settings object from being constructed.  The
    authoritative validation lives in each provider's ``__init__``.
    """

    def test_anthropic_missing_api_key_warns(self, caplog):
        """Anthropic provider warns when no credentials are set."""
        with caplog.at_level(logging.WARNING, logger="open_scientist.settings"):
            settings = ProviderSettings(
                CLAUDE_PROVIDER="anthropic",
                ANTHROPIC_API_KEY=None,
                CLAUDE_CODE_OAUTH_TOKEN=None,
            )
        assert settings.claude_provider == "anthropic"
        assert "ANTHROPIC_API_KEY" in caplog.text

    def test_anthropic_valid_config(self):
        """Valid Anthropic configuration passes validation."""
        settings = ProviderSettings(
            CLAUDE_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="sk-ant-test-key",
        )
        assert settings.claude_provider == "anthropic"
        assert settings.anthropic_api_key == "sk-ant-test-key"

    def test_anthropic_valid_with_oauth_token(self, caplog):
        """Anthropic provider accepts CLAUDE_CODE_OAUTH_TOKEN as alternative."""
        with caplog.at_level(logging.WARNING, logger="open_scientist.settings"):
            settings = ProviderSettings(
                CLAUDE_PROVIDER="anthropic",
                ANTHROPIC_API_KEY=None,
                CLAUDE_CODE_OAUTH_TOKEN="oauth-token-value",
            )
        assert settings.claude_provider == "anthropic"
        assert "ANTHROPIC_API_KEY" not in caplog.text

    def test_cborg_missing_auth_token_warns(self, caplog):
        """CBORG provider warns when ANTHROPIC_AUTH_TOKEN is missing."""
        with caplog.at_level(logging.WARNING, logger="open_scientist.settings"):
            settings = ProviderSettings(
                CLAUDE_PROVIDER="cborg",
                ANTHROPIC_AUTH_TOKEN=None,
            )
        assert settings.claude_provider == "cborg"
        assert "ANTHROPIC_AUTH_TOKEN" in caplog.text

    def test_cborg_missing_base_url_warns(self, caplog):
        """CBORG provider warns when ANTHROPIC_BASE_URL is missing."""
        with caplog.at_level(logging.WARNING, logger="open_scientist.settings"):
            settings = ProviderSettings(
                CLAUDE_PROVIDER="cborg",
                ANTHROPIC_AUTH_TOKEN="test-token",
                ANTHROPIC_BASE_URL=None,
            )
        assert settings.claude_provider == "cborg"
        assert "ANTHROPIC_BASE_URL" in caplog.text

    def test_cborg_valid_config(self):
        """Valid CBORG configuration passes validation."""
        settings = ProviderSettings(
            CLAUDE_PROVIDER="cborg",
            ANTHROPIC_AUTH_TOKEN="test-token",
            ANTHROPIC_BASE_URL="https://api.cborg.lbl.gov",
        )
        assert settings.claude_provider == "cborg"

    def test_vertex_missing_project_id_warns(self, caplog):
        """Vertex AI provider warns when project ID is missing."""
        with (
            patch("os.path.exists", return_value=True),
            caplog.at_level(
                logging.WARNING,
                logger="open_scientist.settings",
            ),
        ):
            settings = ProviderSettings(
                CLAUDE_PROVIDER="vertex",
                ANTHROPIC_VERTEX_PROJECT_ID=None,
                GOOGLE_APPLICATION_CREDENTIALS="/path/to/creds.json",
                GCP_BILLING_ACCOUNT_ID="123-456-789",
                CLOUD_ML_REGION="us-east5",
            )
        assert settings.claude_provider == "vertex"
        assert "ANTHROPIC_VERTEX_PROJECT_ID" in caplog.text

    def test_vertex_missing_credentials_file_warns(self, caplog):
        """Vertex AI provider warns when credentials file is missing."""
        with (
            patch("os.path.exists", return_value=False),
            caplog.at_level(
                logging.WARNING,
                logger="open_scientist.settings",
            ),
        ):
            settings = ProviderSettings(
                CLAUDE_PROVIDER="vertex",
                ANTHROPIC_VERTEX_PROJECT_ID="my-project",
                GOOGLE_APPLICATION_CREDENTIALS="/nonexistent/creds.json",
                GCP_BILLING_ACCOUNT_ID="123-456-789",
                CLOUD_ML_REGION="us-east5",
            )
        assert settings.claude_provider == "vertex"
        assert "not found" in caplog.text

    def test_bedrock_missing_region_warns(self, caplog):
        """Bedrock provider warns when AWS_REGION is missing."""
        with caplog.at_level(logging.WARNING, logger="open_scientist.settings"):
            settings = ProviderSettings(
                CLAUDE_PROVIDER="bedrock",
                AWS_REGION=None,
                AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
                AWS_SECRET_ACCESS_KEY="secret",
            )
        assert settings.claude_provider == "bedrock"
        assert "AWS_REGION" in caplog.text

    def test_bedrock_missing_credentials_warns(self, caplog):
        """Bedrock provider warns when no credential method is set."""
        with caplog.at_level(logging.WARNING, logger="open_scientist.settings"):
            settings = ProviderSettings(
                CLAUDE_PROVIDER="bedrock",
                AWS_REGION="us-east-1",
                AWS_ACCESS_KEY_ID=None,
                AWS_SECRET_ACCESS_KEY=None,
                AWS_PROFILE=None,
                AWS_BEARER_TOKEN_BEDROCK=None,
            )
        assert settings.claude_provider == "bedrock"
        assert "credentials" in caplog.text.lower()

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

    def test_unknown_provider_warns(self, caplog):
        """Unknown provider logs a warning (does not raise)."""
        with caplog.at_level(logging.WARNING, logger="open_scientist.settings"):
            settings = ProviderSettings(CLAUDE_PROVIDER="unknown-provider")
        assert settings.claude_provider == "unknown-provider"
        assert "Unknown provider" in caplog.text

    def test_codex_minimal_config(self):
        """Codex provider has minimal requirements."""
        settings = ProviderSettings(CLAUDE_PROVIDER="codex")
        assert settings.claude_provider == "codex"

    def test_foundry_accepted_as_valid_provider(self, caplog):
        """Foundry is a recognized provider with no warnings."""
        with caplog.at_level(logging.WARNING, logger="open_scientist.settings"):
            settings = ProviderSettings(CLAUDE_PROVIDER="foundry")
        assert settings.claude_provider == "foundry"
        assert caplog.text == ""


class TestProviderContainerEnvVars:
    """Tests for ProviderSettings.get_container_env_vars()."""

    def test_vertex_env_vars_use_container_credentials_path_override(self):
        settings = ProviderSettings(
            CLAUDE_PROVIDER="vertex",
            ANTHROPIC_VERTEX_PROJECT_ID="vertex-proj",
            GOOGLE_APPLICATION_CREDENTIALS="/host/creds.json",
            GCP_BILLING_ACCOUNT_ID="123-456-789",
            CLOUD_ML_REGION="us-east5",
        )

        env = settings.get_container_env_vars(gcp_credentials_container_path="/agent/gcp.json")

        assert env["CLAUDE_PROVIDER"] == "vertex"
        assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/agent/gcp.json"
        assert "CLAUDE_CODE_USE_BEDROCK" not in env

    def test_bedrock_env_vars_include_flag_and_credentials(self):
        settings = ProviderSettings(
            CLAUDE_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
            AWS_SECRET_ACCESS_KEY="secret",
        )

        env = settings.get_container_env_vars()

        assert env["CLAUDE_PROVIDER"] == "bedrock"
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert env["AWS_REGION"] == "us-east-1"
        assert env["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
        assert env["AWS_SECRET_ACCESS_KEY"] == "secret"
        assert "CLAUDE_CODE_USE_VERTEX" not in env

    def test_optional_model_and_token_env_vars_are_included(self):
        settings = ProviderSettings(
            CLAUDE_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="sk-ant-test-key",
            CLAUDE_CODE_OAUTH_TOKEN="oauth-token",
            ANTHROPIC_AUTH_TOKEN="auth-token",
            ANTHROPIC_BASE_URL="https://api.example.com",
            ANTHROPIC_MODEL="model-a",
            ANTHROPIC_SMALL_FAST_MODEL="model-b",
            GITHUB_TOKEN="ghp_example",
        )

        env = settings.get_container_env_vars()

        assert env["CLAUDE_PROVIDER"] == "anthropic"
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-key"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "auth-token"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com"
        assert env["ANTHROPIC_MODEL"] == "model-a"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "model-b"
        assert env["GITHUB_TOKEN"] == "ghp_example"

    def test_foundry_resource_still_exports_api_key(self):
        settings = ProviderSettings(
            CLAUDE_PROVIDER="foundry",
            ANTHROPIC_FOUNDRY_RESOURCE="lab-foundry",
            ANTHROPIC_FOUNDRY_API_KEY="foundry-key",
        )

        env = settings.get_container_env_vars()

        assert env["CLAUDE_PROVIDER"] == "foundry"
        assert env["CLAUDE_CODE_USE_FOUNDRY"] == "1"
        assert env["ANTHROPIC_FOUNDRY_RESOURCE"] == "lab-foundry"
        assert env["ANTHROPIC_FOUNDRY_API_KEY"] == "foundry-key"
        assert "ANTHROPIC_FOUNDRY_BASE_URL" not in env


class TestDatabaseSettings:
    """Tests for database configuration."""

    def test_database_url_required(self, monkeypatch, tmp_path):
        """DATABASE_URL is required."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ValidationError, match="DATABASE_URL"):
            DatabaseSettings()

    def test_effective_database_url(self):
        """effective_database_url returns DATABASE_URL."""
        settings = DatabaseSettings(DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db")
        assert settings.effective_database_url == "postgresql+asyncpg://user:pass@host:5432/db"

    def test_admin_url_falls_back_to_database_url(self, monkeypatch, tmp_path):
        """Admin URL falls back to DATABASE_URL when not set."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ADMIN_DATABASE_URL", raising=False)
        settings = DatabaseSettings(DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db")
        assert settings.effective_admin_database_url == settings.database_url

    def test_admin_url_used_when_set(self, monkeypatch, tmp_path):
        """Admin URL is used when explicitly set."""
        monkeypatch.chdir(tmp_path)
        settings = DatabaseSettings(
            DATABASE_URL="postgresql+asyncpg://user:pass@host:5432/db",
            ADMIN_DATABASE_URL="postgresql+asyncpg://admin:pass@host:5432/db",
        )
        assert (
            settings.effective_admin_database_url == "postgresql+asyncpg://admin:pass@host:5432/db"
        )

    def test_sql_echo_default_false(self):
        """SQL_ECHO defaults to False."""
        settings = DatabaseSettings(DATABASE_URL="postgresql+asyncpg://x:x@localhost/x")
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

    def test_google_oauth_requires_both_id_and_secret(self):
        """Google OAuth requires both client ID and secret."""
        with pytest.raises(ValidationError) as exc_info:
            AuthSettings(
                GOOGLE_CLIENT_ID="test-id",
                GOOGLE_CLIENT_SECRET=None,
            )
        assert "GOOGLE_CLIENT_SECRET" in str(exc_info.value)

    def test_bootstrap_admin_emails_parses_and_normalizes(self):
        """BOOTSTRAP_ADMIN_EMAILS parses comma-separated emails into normalized set."""
        settings = AuthSettings(
            BOOTSTRAP_ADMIN_EMAILS=" Admin@Example.com,other@example.com,admin@example.com ",
        )
        assert settings.bootstrap_admin_emails_set == {
            "admin@example.com",
            "other@example.com",
        }

    def test_bootstrap_admin_emails_rejects_invalid_entry(self):
        """Invalid email entries in BOOTSTRAP_ADMIN_EMAILS should raise validation errors."""
        with pytest.raises(ValidationError) as exc_info:
            AuthSettings(BOOTSTRAP_ADMIN_EMAILS="valid@example.com,not-an-email")
        assert "BOOTSTRAP_ADMIN_EMAILS" in str(exc_info.value)

    def test_bootstrap_admin_emails_defaults_to_empty_set(self):
        """BOOTSTRAP_ADMIN_EMAILS is empty when unset."""
        settings = AuthSettings()
        assert settings.bootstrap_admin_emails_set == set()

    def test_valid_github_oauth(self):
        """Valid GitHub OAuth configuration passes."""
        settings = AuthSettings(
            GITHUB_CLIENT_ID="test-id",
            GITHUB_CLIENT_SECRET="test-secret",
        )
        assert settings.github_client_id == "test-id"
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
        with (
            patch("os.path.exists", return_value=True),
            patch(
                "os.path.isdir",
                return_value=True,
            ),
            pytest.raises(ValidationError) as exc_info,
        ):
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
        with patch("os.path.exists") as mock_exists, patch("os.path.isdir", return_value=True):
            # Directory exists and phenix_env.sh exists
            mock_exists.side_effect = lambda _p: True
            settings = PhenixSettings(PHENIX_PATH="/opt/phenix")
            assert settings.is_available is True

    def test_phenix_not_available_without_env_script(self):
        """is_available is False when phenix_env.sh is missing."""
        with patch("os.path.exists") as mock_exists, patch("os.path.isdir", return_value=True):
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
        assert settings.executor_image == "open_scientist-executor:latest"
        assert settings.executor_memory == "2g"
        assert settings.executor_cpu == 0.5
        assert settings.executor_timeout == 120

    def test_custom_values(self):
        """Custom container settings are applied."""
        settings = ContainerSettings(
            OPEN_SCIENTIST_EXECUTOR_IMAGE="custom-executor:v1",
            OPEN_SCIENTIST_EXECUTOR_MEMORY="4g",
            OPEN_SCIENTIST_EXECUTOR_CPU=1.0,
            OPEN_SCIENTIST_EXECUTOR_TIMEOUT=300,
        )
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
        monkeypatch.delenv("PHENIX_PATH", raising=False)

        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_clear_cache_allows_reload(self, monkeypatch, tmp_path):
        """clear_settings_cache allows settings to be reloaded."""
        # Change to temp directory without .env file to avoid picking up project .env
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PHENIX_PATH", raising=False)

        settings1 = get_settings()
        clear_settings_cache()
        settings2 = get_settings()
        # Different instances (but same values)
        assert settings1 is not settings2
