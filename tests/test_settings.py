"""Tests for centralized settings module."""

import logging
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from openscientist.settings import (
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
        with caplog.at_level(logging.WARNING, logger="openscientist.settings"):
            settings = ProviderSettings(
                OPENSCIENTIST_PROVIDER="anthropic",
                ANTHROPIC_API_KEY=None,
                CLAUDE_CODE_OAUTH_TOKEN=None,
            )
        assert settings.provider_id == "anthropic"
        assert "ANTHROPIC_API_KEY" in caplog.text

    def test_anthropic_valid_config(self):
        """Valid Anthropic configuration passes validation."""
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="sk-ant-test-key",
        )
        assert settings.provider_id == "anthropic"
        assert settings.anthropic_api_key == "sk-ant-test-key"

    def test_anthropic_valid_with_oauth_token(self, caplog):
        """Anthropic provider accepts CLAUDE_CODE_OAUTH_TOKEN as alternative."""
        with caplog.at_level(logging.WARNING, logger="openscientist.settings"):
            settings = ProviderSettings(
                OPENSCIENTIST_PROVIDER="anthropic",
                ANTHROPIC_API_KEY=None,
                CLAUDE_CODE_OAUTH_TOKEN="oauth-token-value",
            )
        assert settings.provider_id == "anthropic"
        assert "ANTHROPIC_API_KEY" not in caplog.text

    def test_cborg_missing_auth_token_warns(self, caplog):
        """CBORG provider warns when ANTHROPIC_AUTH_TOKEN is missing."""
        with caplog.at_level(logging.WARNING, logger="openscientist.settings"):
            settings = ProviderSettings(
                OPENSCIENTIST_PROVIDER="cborg",
                ANTHROPIC_AUTH_TOKEN=None,
            )
        assert settings.provider_id == "cborg"
        assert "ANTHROPIC_AUTH_TOKEN" in caplog.text

    def test_cborg_missing_base_url_warns(self, caplog):
        """CBORG provider warns when ANTHROPIC_BASE_URL is missing."""
        with caplog.at_level(logging.WARNING, logger="openscientist.settings"):
            settings = ProviderSettings(
                OPENSCIENTIST_PROVIDER="cborg",
                ANTHROPIC_AUTH_TOKEN="test-token",
                ANTHROPIC_BASE_URL=None,
            )
        assert settings.provider_id == "cborg"
        assert "ANTHROPIC_BASE_URL" in caplog.text

    def test_cborg_valid_config(self):
        """Valid CBORG configuration passes validation."""
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="cborg",
            ANTHROPIC_AUTH_TOKEN="test-token",
            ANTHROPIC_BASE_URL="https://api.cborg.lbl.gov",
        )
        assert settings.provider_id == "cborg"

    def test_vertex_missing_project_id_warns(self, caplog):
        """Vertex AI provider warns when project ID is missing."""
        with (
            patch("os.path.exists", return_value=True),
            caplog.at_level(
                logging.WARNING,
                logger="openscientist.settings",
            ),
        ):
            settings = ProviderSettings(
                OPENSCIENTIST_PROVIDER="vertex",
                ANTHROPIC_VERTEX_PROJECT_ID=None,
                GOOGLE_APPLICATION_CREDENTIALS="/path/to/creds.json",
                GCP_BILLING_ACCOUNT_ID="123-456-789",
                CLOUD_ML_REGION="us-east5",
            )
        assert settings.provider_id == "vertex"
        assert "ANTHROPIC_VERTEX_PROJECT_ID" in caplog.text

    def test_vertex_missing_credentials_file_warns(self, caplog):
        """Vertex AI provider warns when credentials file is missing."""
        with (
            patch("os.path.exists", return_value=False),
            caplog.at_level(
                logging.WARNING,
                logger="openscientist.settings",
            ),
        ):
            settings = ProviderSettings(
                OPENSCIENTIST_PROVIDER="vertex",
                ANTHROPIC_VERTEX_PROJECT_ID="my-project",
                GOOGLE_APPLICATION_CREDENTIALS="/nonexistent/creds.json",
                GCP_BILLING_ACCOUNT_ID="123-456-789",
                CLOUD_ML_REGION="us-east5",
            )
        assert settings.provider_id == "vertex"
        assert "not found" in caplog.text

    def test_bedrock_missing_region_warns(self, caplog):
        """Bedrock provider warns when AWS_REGION is missing."""
        with caplog.at_level(logging.WARNING, logger="openscientist.settings"):
            settings = ProviderSettings(
                OPENSCIENTIST_PROVIDER="bedrock",
                AWS_REGION=None,
                AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
                AWS_SECRET_ACCESS_KEY="secret",
            )
        assert settings.provider_id == "bedrock"
        assert "AWS_REGION" in caplog.text

    def test_bedrock_missing_credentials_warns(self, caplog):
        """Bedrock provider warns when no credential method is set."""
        with caplog.at_level(logging.WARNING, logger="openscientist.settings"):
            settings = ProviderSettings(
                OPENSCIENTIST_PROVIDER="bedrock",
                AWS_REGION="us-east-1",
                AWS_ACCESS_KEY_ID=None,
                AWS_SECRET_ACCESS_KEY=None,
                AWS_PROFILE=None,
                AWS_BEARER_TOKEN_BEDROCK=None,
            )
        assert settings.provider_id == "bedrock"
        assert "credentials" in caplog.text.lower()

    def test_bedrock_valid_with_access_key(self):
        """Bedrock with access key/secret is valid."""
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
            AWS_SECRET_ACCESS_KEY="secret",
        )
        assert settings.provider_id == "bedrock"

    def test_bedrock_valid_with_profile(self):
        """Bedrock with profile is valid."""
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_PROFILE="default",
        )
        assert settings.provider_id == "bedrock"

    def test_unknown_provider_warns(self, caplog):
        """Unknown provider logs a warning (does not raise)."""
        with caplog.at_level(logging.WARNING, logger="openscientist.settings"):
            settings = ProviderSettings(OPENSCIENTIST_PROVIDER="unknown-provider")
        assert settings.provider_id == "unknown-provider"
        assert "Unknown provider" in caplog.text

    def test_foundry_accepted_as_valid_provider(self, caplog):
        """Foundry is a recognized provider with no warnings."""
        with caplog.at_level(logging.WARNING, logger="openscientist.settings"):
            settings = ProviderSettings(OPENSCIENTIST_PROVIDER="foundry")
        assert settings.provider_id == "foundry"
        assert caplog.text == ""


class TestProviderIdEnvVar:
    """Tests for OPENSCIENTIST_PROVIDER and rejection of the removed CLAUDE_PROVIDER."""

    def test_canonical_env_var_resolves(self, monkeypatch, tmp_path):
        """OPENSCIENTIST_PROVIDER is the canonical env-var name."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CLAUDE_PROVIDER", raising=False)
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
        settings = ProviderSettings()
        assert settings.provider_id == "anthropic"

    def test_legacy_env_var_raises_clear_error(self, monkeypatch, tmp_path):
        """Setting the removed CLAUDE_PROVIDER raises with a rename hint."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENSCIENTIST_PROVIDER", raising=False)
        monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
        with pytest.raises(ValueError, match="CLAUDE_PROVIDER has been renamed"):
            ProviderSettings()

    def test_legacy_env_var_rejected_even_when_canonical_also_set(self, monkeypatch, tmp_path):
        """Both env vars set together still raises so users notice the leftover."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
        monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
        with pytest.raises(ValueError, match="CLAUDE_PROVIDER has been renamed"):
            ProviderSettings()


class TestModelEnvVar:
    """Tests for OPENSCIENTIST_MODEL and rejection of the removed ANTHROPIC_MODEL."""

    def test_canonical_env_var_resolves(self, monkeypatch, tmp_path):
        """OPENSCIENTIST_MODEL is the canonical env-var name."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.setenv("OPENSCIENTIST_MODEL", "claude-sonnet-4-6")
        settings = ProviderSettings()
        assert settings.model == "claude-sonnet-4-6"

    def test_legacy_env_var_raises_clear_error(self, monkeypatch, tmp_path):
        """Setting the removed ANTHROPIC_MODEL raises with a rename hint."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENSCIENTIST_MODEL", raising=False)
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        with pytest.raises(ValueError, match="ANTHROPIC_MODEL has been renamed"):
            ProviderSettings()

    def test_legacy_env_var_rejected_even_when_canonical_also_set(self, monkeypatch, tmp_path):
        """Both env vars set together still raises so users notice the leftover."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("OPENSCIENTIST_MODEL", "claude-sonnet-4-6")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        with pytest.raises(ValueError, match="ANTHROPIC_MODEL has been renamed"):
            ProviderSettings()


class TestModelFormatValidation:
    """Tests that ProviderSettings rejects model ids that do not match the
    selected provider's naming convention."""

    def test_anthropic_accepts_claude_prefix(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="anthropic",
            OPENSCIENTIST_MODEL="claude-sonnet-4-6",
        )
        assert settings.model == "claude-sonnet-4-6"

    def test_anthropic_rejects_non_claude_model(self):
        with pytest.raises(ValueError, match="does not look like an Anthropic model"):
            ProviderSettings(
                OPENSCIENTIST_PROVIDER="anthropic",
                OPENSCIENTIST_MODEL="gpt-5.2",
            )

    def test_cborg_rejects_non_claude_model(self):
        with pytest.raises(ValueError, match="claude-"):
            ProviderSettings(
                OPENSCIENTIST_PROVIDER="cborg",
                OPENSCIENTIST_MODEL="gpt-5.2",
            )

    def test_vertex_accepts_dated_claude_model(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="vertex",
            OPENSCIENTIST_MODEL="claude-sonnet-4-5@20250929",
        )
        assert settings.model == "claude-sonnet-4-5@20250929"

    def test_vertex_rejects_undated_claude_model(self):
        with pytest.raises(ValueError, match="Vertex"):
            ProviderSettings(
                OPENSCIENTIST_PROVIDER="vertex",
                OPENSCIENTIST_MODEL="claude-sonnet-4-6",
            )

    def test_bedrock_accepts_region_prefixed_model(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="bedrock",
            OPENSCIENTIST_MODEL="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        )
        assert settings.model == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    def test_bedrock_accepts_inference_profile_arn(self):
        arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/abcd"
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="bedrock",
            OPENSCIENTIST_MODEL=arn,
        )
        assert settings.model == arn

    def test_bedrock_rejects_bare_claude_model(self):
        with pytest.raises(ValueError, match="Bedrock"):
            ProviderSettings(
                OPENSCIENTIST_PROVIDER="bedrock",
                OPENSCIENTIST_MODEL="claude-sonnet-4-6",
            )

    def test_foundry_does_not_enforce_a_pattern(self):
        """Foundry deployment names are user-defined, so we do not validate."""
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="foundry",
            OPENSCIENTIST_MODEL="any-deployment-name",
        )
        assert settings.model == "any-deployment-name"

    def test_unset_model_skips_validation(self):
        """An unset model is always valid (the provider falls back to defaults)."""
        settings = ProviderSettings(OPENSCIENTIST_PROVIDER="vertex")
        assert settings.model is None


class TestProviderContainerEnvVars:
    """Tests for ProviderSettings.get_container_env_vars()."""

    def test_vertex_env_vars_use_container_credentials_path_override(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="vertex",
            ANTHROPIC_VERTEX_PROJECT_ID="vertex-proj",
            GOOGLE_APPLICATION_CREDENTIALS="/host/creds.json",
            GCP_BILLING_ACCOUNT_ID="123-456-789",
            CLOUD_ML_REGION="us-east5",
        )

        env = settings.get_container_env_vars(gcp_credentials_container_path="/agent/gcp.json")

        assert env["OPENSCIENTIST_PROVIDER"] == "vertex"
        assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert env["GOOGLE_APPLICATION_CREDENTIALS"] == "/agent/gcp.json"
        assert "CLAUDE_CODE_USE_BEDROCK" not in env

    def test_bedrock_env_vars_include_flag_and_credentials(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="bedrock",
            AWS_REGION="us-east-1",
            AWS_ACCESS_KEY_ID="AKIAIOSFODNN7EXAMPLE",
            AWS_SECRET_ACCESS_KEY="secret",
        )

        env = settings.get_container_env_vars()

        assert env["OPENSCIENTIST_PROVIDER"] == "bedrock"
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
        assert env["AWS_REGION"] == "us-east-1"
        assert env["AWS_ACCESS_KEY_ID"] == "AKIAIOSFODNN7EXAMPLE"
        assert env["AWS_SECRET_ACCESS_KEY"] == "secret"
        assert "CLAUDE_CODE_USE_VERTEX" not in env

    def test_openai_api_key_passed_for_codex_provider(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="openai",
            OPENAI_API_KEY="sk-openai-test",
        )

        env = settings.get_container_env_vars()

        assert env["OPENSCIENTIST_PROVIDER"] == "openai"
        assert env["OPENAI_API_KEY"] == "sk-openai-test"

    def test_openai_api_key_omitted_when_unset(self):
        settings = ProviderSettings(OPENSCIENTIST_PROVIDER="openai")
        assert "OPENAI_API_KEY" not in settings.get_container_env_vars()

    def test_azure_openai_vars_passed_for_codex_provider(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="azure-openai",
            AZURE_OPENAI_API_KEY="az-key",
            AZURE_OPENAI_RESOURCE="myres",
            AZURE_OPENAI_DEPLOYMENT="mydep",
            AZURE_OPENAI_API_VERSION="2025-04-01-preview",
        )

        env = settings.get_container_env_vars()

        assert env["OPENSCIENTIST_PROVIDER"] == "azure-openai"
        assert env["AZURE_OPENAI_API_KEY"] == "az-key"
        assert env["AZURE_OPENAI_RESOURCE"] == "myres"
        assert env["AZURE_OPENAI_DEPLOYMENT"] == "mydep"
        assert env["AZURE_OPENAI_API_VERSION"] == "2025-04-01-preview"

    def test_azure_openai_vars_omitted_when_unset(self, monkeypatch, tmp_path):
        # The dev .env reaches tests via both os.environ (database.engine calls
        # load_dotenv() at import) and the settings env_file. Neutralize both:
        # chdir to an empty dir so env_file resolves to nothing, and drop any
        # real Azure values already loaded into os.environ.
        monkeypatch.chdir(tmp_path)
        for var in (
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_RESOURCE",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_API_VERSION",
        ):
            monkeypatch.delenv(var, raising=False)
        settings = ProviderSettings(OPENSCIENTIST_PROVIDER="azure-openai")
        env = settings.get_container_env_vars()
        assert "AZURE_OPENAI_API_KEY" not in env
        assert "AZURE_OPENAI_RESOURCE" not in env

    def test_ollama_vars_passed_for_codex_provider(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="ollama",
            OLLAMA_BASE_URL="http://host.docker.internal:11434/v1",
            OLLAMA_MODEL="gpt-oss:20b",
        )

        env = settings.get_container_env_vars()

        assert env["OPENSCIENTIST_PROVIDER"] == "ollama"
        assert env["OLLAMA_BASE_URL"] == "http://host.docker.internal:11434/v1"
        assert env["OLLAMA_MODEL"] == "gpt-oss:20b"

    def test_ollama_vars_default_when_unset(self, monkeypatch, tmp_path):
        # The dev .env reaches tests via both os.environ (database.engine calls
        # load_dotenv() at import) and the settings env_file. Neutralize both.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
        settings = ProviderSettings(OPENSCIENTIST_PROVIDER="ollama")
        env = settings.get_container_env_vars()
        assert env["OLLAMA_BASE_URL"] == "http://localhost:11434/v1"
        assert env["OLLAMA_MODEL"] == "gpt-oss:20b"

    def test_optional_model_and_token_env_vars_are_included(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="anthropic",
            ANTHROPIC_API_KEY="sk-ant-test-key",
            CLAUDE_CODE_OAUTH_TOKEN="oauth-token",
            ANTHROPIC_AUTH_TOKEN="auth-token",
            ANTHROPIC_BASE_URL="https://api.example.com",
            OPENSCIENTIST_MODEL="claude-sonnet-test",
            ANTHROPIC_SMALL_FAST_MODEL="model-b",
            GITHUB_TOKEN="ghp_example",
        )

        env = settings.get_container_env_vars()

        assert env["OPENSCIENTIST_PROVIDER"] == "anthropic"
        assert env["ANTHROPIC_API_KEY"] == "sk-ant-test-key"
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "auth-token"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com"
        assert env["OPENSCIENTIST_MODEL"] == "claude-sonnet-test"
        assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "model-b"
        assert env["GITHUB_TOKEN"] == "ghp_example"

    def test_foundry_resource_still_exports_api_key(self):
        settings = ProviderSettings(
            OPENSCIENTIST_PROVIDER="foundry",
            ANTHROPIC_FOUNDRY_RESOURCE="lab-foundry",
            ANTHROPIC_FOUNDRY_API_KEY="foundry-key",
        )

        env = settings.get_container_env_vars()

        assert env["OPENSCIENTIST_PROVIDER"] == "foundry"
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

    def test_orcid_oauth_requires_both_id_and_secret(self):
        """ORCID OAuth requires both client ID and secret."""
        with pytest.raises(ValidationError) as exc_info:
            AuthSettings(
                ORCID_CLIENT_ID="APP-TEST1234567890",
                ORCID_CLIENT_SECRET=None,
            )
        assert "ORCID_CLIENT_SECRET" in str(exc_info.value)

    def test_orcid_oauth_requires_id_if_secret_set(self):
        """ORCID OAuth requires ID if secret is set."""
        with pytest.raises(ValidationError) as exc_info:
            AuthSettings(
                ORCID_CLIENT_ID=None,
                ORCID_CLIENT_SECRET="test-secret",
            )
        assert "ORCID_CLIENT_ID" in str(exc_info.value)

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

    def test_valid_orcid_oauth(self):
        """Valid ORCID OAuth configuration passes."""
        settings = AuthSettings(
            ORCID_CLIENT_ID="APP-TEST1234567890",
            ORCID_CLIENT_SECRET="test-secret",
        )
        assert settings.orcid_client_id == "APP-TEST1234567890"
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

    def test_phenix_host_path_absolute_required(self):
        """PHENIX_HOST_PATH must be an absolute path."""
        with pytest.raises(ValidationError) as exc_info:
            PhenixSettings(PHENIX_HOST_PATH="relative/path")
        assert "absolute path" in str(exc_info.value)

    def test_phenix_host_path_no_traversal(self):
        """PHENIX_HOST_PATH must not contain path traversal."""
        with pytest.raises(ValidationError) as exc_info:
            PhenixSettings(PHENIX_HOST_PATH="/opt/../etc/phenix")
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

    def test_phenix_host_path_none_is_valid(self):
        """None PHENIX_HOST_PATH is valid (Phenix is optional)."""
        settings = PhenixSettings(PHENIX_HOST_PATH=None)
        assert settings.phenix_host_path is None

    def test_phenix_is_available_checks_phenix_about(self):
        """is_available is True when bin/phenix.about exists (2.x layout)."""
        with patch("os.path.exists") as mock_exists, patch("os.path.isdir", return_value=True):
            mock_exists.side_effect = lambda _p: True
            settings = PhenixSettings(PHENIX_PATH="/opt/phenix")
            assert settings.is_available is True

    def test_phenix_not_available_without_phenix_about(self):
        """is_available is False when bin/phenix.about is missing."""
        with patch("os.path.exists") as mock_exists, patch("os.path.isdir", return_value=True):
            mock_exists.side_effect = lambda path: "phenix.about" not in path
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

    def test_default_values(self, monkeypatch, tmp_path):
        """Default container settings are reasonable."""
        # The dev .env reaches tests via both os.environ (database.engine calls
        # load_dotenv() at import) and the settings env_file. Neutralize both.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OPENSCIENTIST_AGENT_IMAGE", raising=False)
        monkeypatch.delenv("OPENSCIENTIST_EXECUTOR_IMAGE", raising=False)
        settings = ContainerSettings()
        assert settings.executor_image == "openscientist-executor:latest"
        assert settings.agent_image == "openscientist-agent:latest"
        assert settings.executor_memory == "2g"
        assert settings.executor_cpu == 0.5
        assert settings.executor_timeout == 120

    def test_custom_values(self):
        """Custom container settings are applied."""
        settings = ContainerSettings(
            OPENSCIENTIST_EXECUTOR_IMAGE="custom-executor:v1",
            OPENSCIENTIST_AGENT_IMAGE="custom-agent:v1",
            OPENSCIENTIST_EXECUTOR_MEMORY="4g",
            OPENSCIENTIST_EXECUTOR_CPU=1.0,
            OPENSCIENTIST_EXECUTOR_TIMEOUT=300,
        )
        assert settings.executor_image == "custom-executor:v1"
        assert settings.agent_image == "custom-agent:v1"
        assert settings.executor_memory == "4g"
        assert settings.executor_cpu == 1.0
        assert settings.executor_timeout == 300

    def test_agent_image_picks_up_env_override(self, monkeypatch):
        """OPENSCIENTIST_AGENT_IMAGE env var overrides the default."""
        monkeypatch.setenv("OPENSCIENTIST_AGENT_IMAGE", "openscientist-agent:staging")
        settings = ContainerSettings()
        assert settings.agent_image == "openscientist-agent:staging"


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
