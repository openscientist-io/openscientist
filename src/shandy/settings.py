"""
Centralized settings module for SHANDY.

Validates all environment variables at startup using Pydantic v2 BaseSettings.
Provides clear error messages when configuration is invalid.
"""

import hashlib
import hmac
import logging
import os
import re
from functools import lru_cache

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)
_SIMPLE_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


class DevSettings(BaseSettings):
    """Development mode settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dev_mode: bool = Field(default=False, alias="SHANDY_DEV_MODE")
    simulate_provider_error: bool = Field(default=False, alias="SIMULATE_PROVIDER_ERROR")


class ProviderSettings(BaseSettings):
    """Provider configuration for model access."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Provider selection
    claude_provider: str = Field(
        default="anthropic",
        alias="CLAUDE_PROVIDER",
        description="Provider: anthropic, cborg, vertex, bedrock, codex, foundry",
    )

    # GitHub token for skill syncing
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")

    # Anthropic direct API
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # CBORG (Berkeley Lab) / OAuth tokens
    anthropic_auth_token: str | None = Field(default=None, alias="ANTHROPIC_AUTH_TOKEN")
    claude_code_oauth_token: str | None = Field(default=None, alias="CLAUDE_CODE_OAUTH_TOKEN")
    anthropic_base_url: str | None = Field(default=None, alias="ANTHROPIC_BASE_URL")

    # Model settings
    anthropic_model: str | None = Field(default=None, alias="ANTHROPIC_MODEL")
    anthropic_small_fast_model: str | None = Field(default=None, alias="ANTHROPIC_SMALL_FAST_MODEL")
    anthropic_default_sonnet_model: str | None = Field(
        default=None, alias="ANTHROPIC_DEFAULT_SONNET_MODEL"
    )
    anthropic_default_haiku_model: str | None = Field(
        default=None, alias="ANTHROPIC_DEFAULT_HAIKU_MODEL"
    )
    anthropic_default_opus_model: str | None = Field(
        default=None, alias="ANTHROPIC_DEFAULT_OPUS_MODEL"
    )

    # AWS Bedrock
    aws_region: str | None = Field(default=None, alias="AWS_REGION")
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_profile: str | None = Field(default=None, alias="AWS_PROFILE")
    aws_bearer_token_bedrock: str | None = Field(default=None, alias="AWS_BEARER_TOKEN_BEDROCK")

    # Google Vertex AI
    anthropic_vertex_project_id: str | None = Field(
        default=None, alias="ANTHROPIC_VERTEX_PROJECT_ID"
    )
    google_application_credentials: str | None = Field(
        default=None, alias="GOOGLE_APPLICATION_CREDENTIALS"
    )
    # Host path for GCP credentials (for agent container mounts when running in Docker)
    gcp_credentials_host_path: str | None = Field(default=None, alias="GCP_CREDENTIALS_HOST_PATH")
    gcp_billing_account_id: str | None = Field(default=None, alias="GCP_BILLING_ACCOUNT_ID")
    cloud_ml_region: str | None = Field(default=None, alias="CLOUD_ML_REGION")
    vertex_region_claude_4_5_sonnet: str | None = Field(
        default=None, alias="VERTEX_REGION_CLAUDE_4_5_SONNET"
    )
    vertex_region_claude_4_5_haiku: str | None = Field(
        default=None, alias="VERTEX_REGION_CLAUDE_4_5_HAIKU"
    )

    # Azure AI Foundry
    anthropic_foundry_resource: str | None = Field(default=None, alias="ANTHROPIC_FOUNDRY_RESOURCE")
    anthropic_foundry_base_url: str | None = Field(default=None, alias="ANTHROPIC_FOUNDRY_BASE_URL")
    anthropic_foundry_api_key: str | None = Field(default=None, alias="ANTHROPIC_FOUNDRY_API_KEY")
    # Azure Cost Management (optional, enables billing data for foundry provider)
    azure_subscription_id: str | None = Field(default=None, alias="AZURE_SUBSCRIPTION_ID")
    azure_resource_group: str | None = Field(default=None, alias="AZURE_RESOURCE_GROUP")

    @staticmethod
    def _warn_if_missing(value: str | None, message: str, warnings: list[str]) -> None:
        if not value:
            warnings.append(message)

    def _anthropic_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.anthropic_api_key and not self.claude_code_oauth_token:
            warnings.append(
                "ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN is required "
                "when CLAUDE_PROVIDER=anthropic. "
                "Get your API key from https://console.anthropic.com "
                "or run 'claude login' for OAuth."
            )
        return warnings

    def _cborg_warnings(self) -> list[str]:
        warnings: list[str] = []
        self._warn_if_missing(
            self.anthropic_auth_token,
            "ANTHROPIC_AUTH_TOKEN is required when CLAUDE_PROVIDER=cborg",
            warnings,
        )
        self._warn_if_missing(
            self.anthropic_base_url,
            "ANTHROPIC_BASE_URL is required when CLAUDE_PROVIDER=cborg "
            "(should be https://api.cborg.lbl.gov)",
            warnings,
        )
        return warnings

    def _vertex_warnings(self) -> list[str]:
        warnings: list[str] = []
        self._warn_if_missing(
            self.anthropic_vertex_project_id,
            "ANTHROPIC_VERTEX_PROJECT_ID is required for Vertex AI",
            warnings,
        )
        if not self.google_application_credentials:
            warnings.append(
                "GOOGLE_APPLICATION_CREDENTIALS is required for Vertex AI "
                "(path to service account JSON)"
            )
        elif not os.path.exists(os.path.expanduser(self.google_application_credentials)):
            warnings.append(
                f"GOOGLE_APPLICATION_CREDENTIALS file not found: "
                f"{self.google_application_credentials}"
            )
        self._warn_if_missing(
            self.gcp_billing_account_id,
            "GCP_BILLING_ACCOUNT_ID is required for Vertex AI cost tracking",
            warnings,
        )
        self._warn_if_missing(
            self.cloud_ml_region,
            "CLOUD_ML_REGION is required for Vertex AI (e.g., us-east5)",
            warnings,
        )
        return warnings

    def _bedrock_warnings(self) -> list[str]:
        warnings: list[str] = []
        self._warn_if_missing(
            self.aws_region,
            "AWS_REGION is required for Bedrock (e.g., us-east-1)",
            warnings,
        )
        has_access_key = self.aws_access_key_id and self.aws_secret_access_key
        has_profile = bool(self.aws_profile)
        has_bearer = bool(self.aws_bearer_token_bedrock)
        if not (has_access_key or has_profile or has_bearer):
            warnings.append(
                "AWS credentials required for Bedrock. Set one of: "
                "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_PROFILE, "
                "or AWS_BEARER_TOKEN_BEDROCK"
            )
        return warnings

    @staticmethod
    def _unknown_provider_warnings(provider: str) -> list[str]:
        return [
            f"Unknown provider '{provider}'. "
            "Valid options: anthropic, cborg, vertex, bedrock, codex, foundry"
        ]

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> "ProviderSettings":
        """Warn about missing provider config.

        This is intentionally warn-only so that settings can always be
        constructed (e.g. during testing or when only a subset of env vars
        is available).  The authoritative validation lives in each
        provider's ``_validate_required_config``.
        """
        from collections.abc import Callable

        provider = self.claude_provider.lower()
        warning_builders: dict[str, Callable[[], list[str]]] = {
            "anthropic": self._anthropic_warnings,
            "cborg": self._cborg_warnings,
            "vertex": self._vertex_warnings,
            "bedrock": self._bedrock_warnings,
            "codex": lambda: [],
            "foundry": lambda: [],
        }
        warnings = warning_builders.get(
            provider, lambda: self._unknown_provider_warnings(provider)
        )()
        for warning in warnings:
            logger.warning("Provider config: %s", warning)

        return self

    @staticmethod
    def _set_env_if_present(env_vars: dict[str, str], key: str, value: str | None) -> None:
        if value:
            env_vars[key] = value

    def _apply_model_env_vars(self, env_vars: dict[str, str]) -> None:
        self._set_env_if_present(env_vars, "ANTHROPIC_MODEL", self.anthropic_model)
        self._set_env_if_present(
            env_vars, "ANTHROPIC_SMALL_FAST_MODEL", self.anthropic_small_fast_model
        )
        self._set_env_if_present(
            env_vars, "ANTHROPIC_DEFAULT_SONNET_MODEL", self.anthropic_default_sonnet_model
        )
        self._set_env_if_present(
            env_vars, "ANTHROPIC_DEFAULT_HAIKU_MODEL", self.anthropic_default_haiku_model
        )
        self._set_env_if_present(
            env_vars, "ANTHROPIC_DEFAULT_OPUS_MODEL", self.anthropic_default_opus_model
        )

    def _apply_auth_env_vars(self, env_vars: dict[str, str]) -> None:
        self._set_env_if_present(env_vars, "ANTHROPIC_API_KEY", self.anthropic_api_key)
        self._set_env_if_present(env_vars, "ANTHROPIC_AUTH_TOKEN", self.anthropic_auth_token)
        self._set_env_if_present(env_vars, "CLAUDE_CODE_OAUTH_TOKEN", self.claude_code_oauth_token)
        self._set_env_if_present(env_vars, "ANTHROPIC_BASE_URL", self.anthropic_base_url)

    def _apply_vertex_env_vars(
        self,
        env_vars: dict[str, str],
        gcp_credentials_container_path: str | None,
    ) -> None:
        if self.claude_provider.lower() == "vertex":
            env_vars["CLAUDE_CODE_USE_VERTEX"] = "1"
        self._set_env_if_present(
            env_vars, "ANTHROPIC_VERTEX_PROJECT_ID", self.anthropic_vertex_project_id
        )
        self._set_env_if_present(env_vars, "GCP_BILLING_ACCOUNT_ID", self.gcp_billing_account_id)
        self._set_env_if_present(env_vars, "CLOUD_ML_REGION", self.cloud_ml_region)
        self._set_env_if_present(
            env_vars, "VERTEX_REGION_CLAUDE_4_5_SONNET", self.vertex_region_claude_4_5_sonnet
        )
        self._set_env_if_present(
            env_vars, "VERTEX_REGION_CLAUDE_4_5_HAIKU", self.vertex_region_claude_4_5_haiku
        )
        if self.google_application_credentials:
            env_vars["GOOGLE_APPLICATION_CREDENTIALS"] = (
                gcp_credentials_container_path or self.google_application_credentials
            )

    def _apply_bedrock_env_vars(self, env_vars: dict[str, str]) -> None:
        if self.claude_provider.lower() == "bedrock":
            env_vars["CLAUDE_CODE_USE_BEDROCK"] = "1"
        self._set_env_if_present(env_vars, "AWS_REGION", self.aws_region)
        self._set_env_if_present(env_vars, "AWS_ACCESS_KEY_ID", self.aws_access_key_id)
        self._set_env_if_present(env_vars, "AWS_SECRET_ACCESS_KEY", self.aws_secret_access_key)
        self._set_env_if_present(env_vars, "AWS_PROFILE", self.aws_profile)
        self._set_env_if_present(
            env_vars, "AWS_BEARER_TOKEN_BEDROCK", self.aws_bearer_token_bedrock
        )

    def _apply_foundry_env_vars(self, env_vars: dict[str, str]) -> None:
        if self.claude_provider.lower() == "foundry":
            env_vars["CLAUDE_CODE_USE_FOUNDRY"] = "1"

        self._set_env_if_present(
            env_vars, "ANTHROPIC_FOUNDRY_RESOURCE", self.anthropic_foundry_resource
        )
        # Claude Code treats resource/base_url as mutually exclusive.
        if not self.anthropic_foundry_resource:
            self._set_env_if_present(
                env_vars, "ANTHROPIC_FOUNDRY_BASE_URL", self.anthropic_foundry_base_url
            )
        self._set_env_if_present(
            env_vars, "ANTHROPIC_FOUNDRY_API_KEY", self.anthropic_foundry_api_key
        )

    def get_container_env_vars(
        self,
        gcp_credentials_container_path: str | None = None,
    ) -> dict[str, str]:
        """
        Get environment variables to pass to agent containers.

        Args:
            gcp_credentials_container_path: Container path for GCP credentials file.

        Returns:
            Dict of env var names to values (only includes set values).
        """
        env_vars: dict[str, str] = {"CLAUDE_PROVIDER": self.claude_provider}
        self._apply_model_env_vars(env_vars)
        self._apply_auth_env_vars(env_vars)
        self._apply_vertex_env_vars(env_vars, gcp_credentials_container_path)
        self._apply_bedrock_env_vars(env_vars)
        self._apply_foundry_env_vars(env_vars)
        self._set_env_if_present(env_vars, "GITHUB_TOKEN", self.github_token)
        return env_vars


class DatabaseSettings(BaseSettings):
    """Database configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(alias="DATABASE_URL")

    # Admin database URL for elevated operations (bypasses RLS via DB role).
    # If not set, falls back to DATABASE_URL.
    admin_database_url: str | None = Field(default=None, alias="ADMIN_DATABASE_URL")

    # Debug settings
    sql_echo: bool = Field(default=False, alias="SQL_ECHO")

    @property
    def effective_database_url(self) -> str:
        """Get the database URL."""
        return self.database_url

    @property
    def effective_admin_database_url(self) -> str:
        """Get the admin database URL for elevated operations (bypasses RLS)."""
        return self.admin_database_url or self.database_url


class AuthSettings(BaseSettings):
    """Authentication configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # General auth settings
    app_url: str = Field(default="http://localhost:8080", alias="APP_URL")
    session_duration_days: int = Field(default=30, alias="SESSION_DURATION_DAYS")

    # Derived from SHANDY_SECRET_KEY (populated by Settings.derive_secrets)
    storage_secret: str = Field(default="")
    token_encryption_key: str | None = Field(default=None)

    # Google OAuth
    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")

    # GitHub OAuth
    github_client_id: str | None = Field(default=None, alias="GITHUB_CLIENT_ID")
    github_client_secret: str | None = Field(default=None, alias="GITHUB_CLIENT_SECRET")
    bootstrap_admin_emails: str | None = Field(default=None, alias="BOOTSTRAP_ADMIN_EMAILS")

    @staticmethod
    def _parse_bootstrap_admin_emails(raw_value: str | None) -> set[str]:
        """Parse and validate BOOTSTRAP_ADMIN_EMAILS as normalized email set."""
        if not raw_value:
            return set()

        emails: set[str] = set()
        for token in raw_value.split(","):
            normalized = token.strip().lower()
            if not normalized:
                continue
            if not _SIMPLE_EMAIL_RE.fullmatch(normalized):
                raise ValueError(
                    "BOOTSTRAP_ADMIN_EMAILS must be a comma-separated list of email addresses; "
                    f"invalid value: '{token.strip()}'"
                )
            emails.add(normalized)
        return emails

    @field_validator("bootstrap_admin_emails")
    @classmethod
    def validate_bootstrap_admin_emails(cls, value: str | None) -> str | None:
        """Validate BOOTSTRAP_ADMIN_EMAILS format if set."""
        cls._parse_bootstrap_admin_emails(value)
        return value

    @model_validator(mode="after")
    def validate_oauth_pairs(self) -> "AuthSettings":
        """Validate that OAuth client ID and secret are paired."""
        errors = []

        if self.google_client_id and not self.google_client_secret:
            errors.append("GOOGLE_CLIENT_SECRET is required when GOOGLE_CLIENT_ID is set")
        if self.google_client_secret and not self.google_client_id:
            errors.append("GOOGLE_CLIENT_ID is required when GOOGLE_CLIENT_SECRET is set")

        if self.github_client_id and not self.github_client_secret:
            errors.append("GITHUB_CLIENT_SECRET is required when GITHUB_CLIENT_ID is set")
        if self.github_client_secret and not self.github_client_id:
            errors.append("GITHUB_CLIENT_ID is required when GITHUB_CLIENT_SECRET is set")

        if errors:
            raise ValueError(
                "OAuth configuration errors:\n" + "\n".join(f"  - {e}" for e in errors)
            )

        return self

    @property
    def is_oauth_configured(self) -> bool:
        """Check if at least one OAuth provider is configured."""
        return bool(self.google_client_id or self.github_client_id)

    @property
    def bootstrap_admin_emails_set(self) -> set[str]:
        """Get BOOTSTRAP_ADMIN_EMAILS as normalized email set."""
        return self._parse_bootstrap_admin_emails(self.bootstrap_admin_emails)


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
    def validate_positive(cls, v: float, info: ValidationInfo) -> float:
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

    # Agent container resource limits
    agent_memory: str = Field(default="8g", alias="SHANDY_AGENT_MEMORY")
    agent_cpu: float = Field(default=2.0, alias="SHANDY_AGENT_CPU")

    # Host path mapping for sibling container volume mounts (executor containers)
    # When the main container runs inside Docker and spawns sibling containers,
    # paths need to be translated from container paths to host paths.
    # Example: /app inside container maps to /home/user/shandy on host
    container_app_dir: str = Field(default="/app", alias="SHANDY_CONTAINER_APP_DIR")
    host_project_dir: str | None = Field(
        default=None,
        alias="SHANDY_HOST_PROJECT_DIR",
        description="Host path for project directory. Required when using agent containers.",
    )


class PhenixSettings(BaseSettings):
    """Phenix structural biology tools configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    phenix_path: str | None = Field(default=None, alias="PHENIX_PATH")

    @field_validator("phenix_path")
    @classmethod
    def validate_phenix_path(cls, v: str | None) -> str | None:
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

    dremio_user: str | None = Field(default=None, alias="DREMIO_USER")
    dremio_password: str | None = Field(default=None, alias="DREMIO_PASSWORD")


class AgentSettings(BaseSettings):
    """Agent behavior configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    max_agent_skills: int = Field(
        default=10,
        alias="MAX_AGENT_SKILLS",
        description="Maximum number of skills an agent can use per job",
    )

    @field_validator("max_agent_skills")
    @classmethod
    def validate_max_agent_skills(cls, v: int) -> int:
        """Validate that max_agent_skills is positive."""
        if v <= 0:
            raise ValueError(f"MAX_AGENT_SKILLS must be positive, got {v}")
        return v


class Settings(BaseSettings):
    """Root settings class with all configuration sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Master secret — all auth secrets are derived from this via HMAC-SHA256
    secret_key: str = Field(alias="SHANDY_SECRET_KEY")

    # Server settings
    port: int = Field(default=8080, alias="PORT")
    max_concurrent_jobs: int = Field(default=1, alias="SHANDY_MAX_CONCURRENT_JOBS")
    base_url: str = Field(
        default="http://localhost:8080",
        alias="SHANDY_BASE_URL",
        description="Base URL for SHANDY (used in notifications and share links)",
    )

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
    agent: AgentSettings = Field(default_factory=AgentSettings)

    @model_validator(mode="after")
    def derive_secrets(self) -> "Settings":
        """Derive auth secrets from the master SHANDY_SECRET_KEY via HMAC-SHA256."""
        key = self.secret_key.encode()
        self.auth.storage_secret = hmac.new(key, b"storage_secret", hashlib.sha256).hexdigest()
        self.auth.token_encryption_key = hmac.new(
            key, b"token_encryption_key", hashlib.sha256
        ).hexdigest()
        return self


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
