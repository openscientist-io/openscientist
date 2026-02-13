"""
Centralized settings module for SHANDY.

Validates all environment variables at startup using Pydantic v2 BaseSettings.
Provides clear error messages when configuration is invalid.
"""

import logging
import os
from functools import lru_cache
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class DevSettings(BaseSettings):
    """Development mode settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dev_mode: bool = Field(default=False, alias="SHANDY_DEV_MODE")


class ProviderSettings(BaseSettings):
    """Provider configuration for model access."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Provider selection
    claude_provider: str = Field(
        default="cborg",
        alias="CLAUDE_PROVIDER",
        description="Provider: anthropic, cborg, vertex, bedrock, codex",
    )

    # Claude CLI path
    claude_cli_path: str = Field(default="claude", alias="CLAUDE_CLI_PATH")

    # GitHub token for skill syncing
    github_token: Optional[str] = Field(default=None, alias="GITHUB_TOKEN")

    # Anthropic direct API
    anthropic_api_key: Optional[str] = Field(default=None, alias="ANTHROPIC_API_KEY")

    # CBORG (Berkeley Lab)
    anthropic_auth_token: Optional[str] = Field(default=None, alias="ANTHROPIC_AUTH_TOKEN")
    anthropic_base_url: Optional[str] = Field(default=None, alias="ANTHROPIC_BASE_URL")

    # Model settings
    anthropic_model: Optional[str] = Field(default=None, alias="ANTHROPIC_MODEL")
    anthropic_small_fast_model: Optional[str] = Field(
        default=None, alias="ANTHROPIC_SMALL_FAST_MODEL"
    )

    # AWS Bedrock
    aws_region: Optional[str] = Field(default=None, alias="AWS_REGION")
    aws_access_key_id: Optional[str] = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_profile: Optional[str] = Field(default=None, alias="AWS_PROFILE")
    aws_bearer_token_bedrock: Optional[str] = Field(default=None, alias="AWS_BEARER_TOKEN_BEDROCK")

    # Google Vertex AI
    anthropic_vertex_project_id: Optional[str] = Field(
        default=None, alias="ANTHROPIC_VERTEX_PROJECT_ID"
    )
    google_application_credentials: Optional[str] = Field(
        default=None, alias="GOOGLE_APPLICATION_CREDENTIALS"
    )
    gcp_billing_account_id: Optional[str] = Field(default=None, alias="GCP_BILLING_ACCOUNT_ID")
    cloud_ml_region: Optional[str] = Field(default=None, alias="CLOUD_ML_REGION")
    vertex_region_claude_4_5_sonnet: Optional[str] = Field(
        default=None, alias="VERTEX_REGION_CLAUDE_4_5_SONNET"
    )
    vertex_region_claude_4_5_haiku: Optional[str] = Field(
        default=None, alias="VERTEX_REGION_CLAUDE_4_5_HAIKU"
    )

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> "ProviderSettings":
        """Validate that required vars are set for the selected provider."""
        provider = self.claude_provider.lower()
        errors = []

        if provider == "anthropic":
            if not self.anthropic_api_key:
                errors.append(
                    "ANTHROPIC_API_KEY is required when CLAUDE_PROVIDER=anthropic. "
                    "Get your API key from https://console.anthropic.com"
                )

        elif provider == "cborg":
            if not self.anthropic_auth_token:
                errors.append("ANTHROPIC_AUTH_TOKEN is required when CLAUDE_PROVIDER=cborg")
            if not self.anthropic_base_url:
                errors.append(
                    "ANTHROPIC_BASE_URL is required when CLAUDE_PROVIDER=cborg "
                    "(should be https://api.cborg.lbl.gov)"
                )

        elif provider == "vertex":
            if not self.anthropic_vertex_project_id:
                errors.append("ANTHROPIC_VERTEX_PROJECT_ID is required for Vertex AI")
            if not self.google_application_credentials:
                errors.append(
                    "GOOGLE_APPLICATION_CREDENTIALS is required for Vertex AI "
                    "(path to service account JSON)"
                )
            elif not os.path.exists(os.path.expanduser(self.google_application_credentials)):
                errors.append(
                    f"GOOGLE_APPLICATION_CREDENTIALS file not found: "
                    f"{self.google_application_credentials}"
                )
            if not self.gcp_billing_account_id:
                errors.append("GCP_BILLING_ACCOUNT_ID is required for Vertex AI cost tracking")
            if not self.cloud_ml_region:
                errors.append("CLOUD_ML_REGION is required for Vertex AI (e.g., us-east5)")

        elif provider == "bedrock":
            if not self.aws_region:
                errors.append("AWS_REGION is required for Bedrock (e.g., us-east-1)")

            has_access_key = self.aws_access_key_id and self.aws_secret_access_key
            has_profile = bool(self.aws_profile)
            has_bearer = bool(self.aws_bearer_token_bedrock)

            if not (has_access_key or has_profile or has_bearer):
                errors.append(
                    "AWS credentials required for Bedrock. Set one of: "
                    "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, "
                    "or AWS_BEARER_TOKEN_BEDROCK"
                )

        elif provider == "codex":
            pass  # Codex provider has minimal requirements

        else:
            errors.append(
                f"Unknown provider '{provider}'. "
                "Valid options: anthropic, cborg, vertex, bedrock, codex"
            )

        if errors:
            raise ValueError(
                "Provider configuration errors:\n" + "\n".join(f"  - {e}" for e in errors)
            )

        return self


class DatabaseSettings(BaseSettings):
    """Database configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: Optional[str] = Field(default=None, alias="DATABASE_URL")

    # Individual components (used if DATABASE_URL not set)
    postgres_host: str = Field(default="localhost", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_user: str = Field(default="shandy", alias="POSTGRES_USER")
    postgres_password: str = Field(default="", alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="shandy", alias="POSTGRES_DB")

    # Debug settings
    sql_echo: bool = Field(default=False, alias="SQL_ECHO")

    @property
    def effective_database_url(self) -> str:
        """Get the database URL, constructing from components if needed."""
        if self.database_url:
            return self.database_url

        if not self.postgres_password:
            raise ValueError(
                "DATABASE_URL or POSTGRES_PASSWORD must be set.\n\n"
                "To fix this:\n"
                "1. Copy .env.example to .env: cp .env.example .env\n"
                "2. Configure DATABASE_URL or POSTGRES_* variables in .env\n"
                "   For local development: DATABASE_URL=postgresql+asyncpg://shandy:shandy_dev_password@localhost:5434/shandy\n"
                "3. Make sure PostgreSQL is running (use 'make dev-start' for Docker setup)\n\n"
                "See CONTRIBUTING.md for complete setup instructions."
            )

        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


class AuthSettings(BaseSettings):
    """Authentication configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # General auth settings
    app_url: str = Field(default="http://localhost:8080", alias="APP_URL")
    storage_secret: str = Field(
        default="change-this-to-a-random-secret-string-in-production",
        alias="STORAGE_SECRET",
    )

    # Session settings
    session_secret: Optional[str] = Field(default=None, alias="SESSION_SECRET")
    session_max_age: int = Field(default=86400, alias="SESSION_MAX_AGE")
    session_duration_days: int = Field(default=30, alias="SESSION_DURATION_DAYS")

    # GitHub OAuth
    github_client_id: Optional[str] = Field(default=None, alias="GITHUB_CLIENT_ID")
    github_client_secret: Optional[str] = Field(default=None, alias="GITHUB_CLIENT_SECRET")

    # ORCID OAuth
    orcid_client_id: Optional[str] = Field(default=None, alias="ORCID_CLIENT_ID")
    orcid_client_secret: Optional[str] = Field(default=None, alias="ORCID_CLIENT_SECRET")
    orcid_api_base: str = Field(default="https://orcid.org", alias="ORCID_API_BASE")

    # Development/testing
    enable_mock_auth: bool = Field(default=False, alias="ENABLE_MOCK_AUTH")

    # Encryption
    token_encryption_key: Optional[str] = Field(default=None, alias="TOKEN_ENCRYPTION_KEY")

    @model_validator(mode="after")
    def validate_oauth_pairs(self) -> "AuthSettings":
        """Validate that OAuth client ID and secret are paired."""
        errors = []

        if self.github_client_id and not self.github_client_secret:
            errors.append("GITHUB_CLIENT_SECRET is required when GITHUB_CLIENT_ID is set")
        if self.github_client_secret and not self.github_client_id:
            errors.append("GITHUB_CLIENT_ID is required when GITHUB_CLIENT_SECRET is set")

        if self.orcid_client_id and not self.orcid_client_secret:
            errors.append("ORCID_CLIENT_SECRET is required when ORCID_CLIENT_ID is set")
        if self.orcid_client_secret and not self.orcid_client_id:
            errors.append("ORCID_CLIENT_ID is required when ORCID_CLIENT_SECRET is set")

        if errors:
            raise ValueError(
                "OAuth configuration errors:\n" + "\n".join(f"  - {e}" for e in errors)
            )

        return self

    @property
    def is_oauth_configured(self) -> bool:
        """Check if at least one OAuth provider is configured."""
        return bool(self.github_client_id or self.orcid_client_id or self.enable_mock_auth)


class BudgetSettings(BaseSettings):
    """Budget and cost tracking configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_project_spend_warn: float = Field(default=100.0, alias="MAX_PROJECT_SPEND_WARN")
    max_project_spend_hard: float = Field(default=500.0, alias="MAX_PROJECT_SPEND_HARD")
    max_job_cost_usd: float = Field(default=10.0, alias="MAX_JOB_COST_USD")
    app_max_budget_usd: float = Field(default=1000.0, alias="APP_MAX_BUDGET_USD")

    # Provider-agnostic budget limits (used by check_budget_limits)
    max_project_spend_total_usd: float = Field(
        default=float("inf"), alias="MAX_PROJECT_SPEND_TOTAL_USD"
    )
    max_project_spend_24h_usd: float = Field(
        default=float("inf"), alias="MAX_PROJECT_SPEND_24H_USD"
    )
    warn_project_spend_24h_usd: float = Field(
        default=float("inf"), alias="WARN_PROJECT_SPEND_24H_USD"
    )

    @field_validator(
        "max_project_spend_warn",
        "max_project_spend_hard",
        "max_job_cost_usd",
        "app_max_budget_usd",
    )
    @classmethod
    def validate_positive(cls, v: float, info) -> float:
        """Validate that budget values are positive."""
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v


class FileSettings(BaseSettings):
    """File handling configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_file_size_mb: int = Field(default=1000, alias="MAX_FILE_SIZE_MB")

    @field_validator("max_file_size_mb")
    @classmethod
    def validate_positive(cls, v: int) -> int:
        """Validate that file size is positive."""
        if v <= 0:
            raise ValueError(f"MAX_FILE_SIZE_MB must be positive, got {v}")
        return v


class ContainerSettings(BaseSettings):
    """Container isolation configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    use_container_isolation: bool = Field(default=False, alias="SHANDY_USE_CONTAINER_ISOLATION")
    executor_image: str = Field(default="shandy-executor:latest", alias="SHANDY_EXECUTOR_IMAGE")
    executor_memory: str = Field(default="2g", alias="SHANDY_EXECUTOR_MEMORY")
    executor_cpu: float = Field(default=0.5, alias="SHANDY_EXECUTOR_CPU")
    executor_timeout: int = Field(default=120, alias="SHANDY_EXECUTOR_TIMEOUT")


class PhenixSettings(BaseSettings):
    """Phenix structural biology tools configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    phenix_path: Optional[str] = Field(default=None, alias="PHENIX_PATH")

    @field_validator("phenix_path")
    @classmethod
    def validate_phenix_path(cls, v: Optional[str]) -> Optional[str]:
        """
        Validate PHENIX_PATH format if set.

        Only validates format (absolute path, no traversal). Existence is
        checked by `is_available` property to match the original behavior
        of phenix_setup.py where invalid paths return None rather than raise.
        """
        if v is None or v == "":
            return None

        # Must be an absolute path
        if not v.startswith("/"):
            raise ValueError(
                f"PHENIX_PATH must be an absolute path (starting with '/'), got: '{v}'\n"
                f"  Example: PHENIX_PATH=/opt/phenix-1.21.2-5419"
            )

        # Must not contain path traversal
        if ".." in v:
            raise ValueError(f"PHENIX_PATH must not contain path traversal (..): '{v}'")

        return v

    @property
    def is_available(self) -> bool:
        """
        Check if Phenix is configured and available.

        This checks for actual existence on the filesystem, complementing
        the format validation done by the validator.
        """
        if not self.phenix_path:
            return False
        # Check directory exists and contains phenix_env.sh
        if not os.path.isdir(self.phenix_path):
            return False
        env_script = os.path.join(self.phenix_path, "phenix_env.sh")
        return os.path.exists(env_script)


class BerkeleyLabSettings(BaseSettings):
    """Berkeley Lab data lakehouse configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kbase_token: Optional[str] = Field(default=None, alias="KBASE_TOKEN")
    dremio_user: Optional[str] = Field(default=None, alias="DREMIO_USER")
    dremio_password: Optional[str] = Field(default=None, alias="DREMIO_PASSWORD")


class Settings(BaseSettings):
    """Root settings class with all configuration sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Server settings
    port: int = Field(default=8080, alias="PORT")

    # Nested settings
    dev: DevSettings = Field(default_factory=DevSettings)
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    file: FileSettings = Field(default_factory=FileSettings)
    container: ContainerSettings = Field(default_factory=ContainerSettings)
    phenix: PhenixSettings = Field(default_factory=PhenixSettings)
    berkeley_lab: BerkeleyLabSettings = Field(default_factory=BerkeleyLabSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Validate and return settings singleton.

    This function validates all environment variables at first call
    and caches the result. Subsequent calls return the cached instance.

    Returns:
        Settings: Validated settings object

    Raises:
        ValidationError: If any environment variables are invalid
    """
    return Settings()


def clear_settings_cache() -> None:
    """Clear the settings cache (useful for testing)."""
    get_settings.cache_clear()
