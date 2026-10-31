"""Centralized settings module for OpenScientist."""

import hashlib
import hmac
import logging
import os
import re
from functools import lru_cache

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

logger = logging.getLogger(__name__)
_SIMPLE_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_VALID_HARNESSES = ("auto", "claude_code", "codex", "omp")


class DevSettings(BaseSettings):
    """Development mode settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    dev_mode: bool = Field(default=False, alias="OPENSCIENTIST_DEV_MODE")
    simulate_provider_error: bool = Field(default=False, alias="SIMULATE_PROVIDER_ERROR")


class ProviderSettings(BaseSettings):
    """Provider configuration for model access."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    provider_id: str = Field(
        default="",
        alias="OPENSCIENTIST_PROVIDER",
        description=(
            "Provider id (anthropic, cborg, vertex, bedrock, foundry, openai, "
            "azure-openai, ollama, vllm, llamacpp). Required: there is no default provider."
        ),
    )

    # Harness (coding-agent runtime), orthogonal to the provider. "auto" derives
    # from the provider family; "omp" drives any provider; "claude_code"/"codex"
    # require a matching family (validated in the factory).
    harness: str = Field(
        default="auto",
        alias="OPENSCIENTIST_HARNESS",
        description=(
            "Agent harness: auto (default, derived from provider), claude_code, codex, or omp."
        ),
    )

    @field_validator("harness")
    @classmethod
    def _validate_harness(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _VALID_HARNESSES:
            raise ValueError(
                f"OPENSCIENTIST_HARNESS={value!r} is not valid. "
                "Choose one of: auto, claude_code, codex, omp."
            )
        return normalized

    # GitHub token for skill syncing
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")

    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    # CBORG (Berkeley Lab)
    anthropic_auth_token: str | None = Field(default=None, alias="ANTHROPIC_AUTH_TOKEN")
    claude_code_oauth_token: str | None = Field(default=None, alias="CLAUDE_CODE_OAUTH_TOKEN")
    anthropic_base_url: str | None = Field(default=None, alias="ANTHROPIC_BASE_URL")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")

    # Azure OpenAI Service as a Codex backend. OPENSCIENTIST_MODEL names the
    # Azure deployment, which Azure routes on as the request-body model.
    azure_openai_api_key: str | None = Field(default=None, alias="AZURE_OPENAI_API_KEY")
    azure_openai_resource: str | None = Field(default=None, alias="AZURE_OPENAI_RESOURCE")
    azure_openai_api_version: str | None = Field(default=None, alias="AZURE_OPENAI_API_VERSION")
    # How many times codex reconnects through Azure's intermittent streaming
    # disconnects before failing the turn
    # (https://github.com/openai/codex/issues/9936). Raise it for lower-capacity
    # Azure deployments that disconnect more often.
    azure_openai_stream_max_retries: int = Field(
        default=10, alias="AZURE_OPENAI_STREAM_MAX_RETRIES"
    )

    # Ollama. Local and keyless.
    ollama_base_url: str = Field(default="http://localhost:11434/v1", alias="OLLAMA_BASE_URL")

    vllm_base_url: str = Field(default="http://localhost:8000/v1", alias="VLLM_BASE_URL")
    vllm_api_key: str | None = Field(default=None, alias="VLLM_API_KEY")

    llamacpp_base_url: str = Field(default="http://localhost:8080/v1", alias="LLAMACPP_BASE_URL")
    llamacpp_api_key: str | None = Field(default=None, alias="LLAMACPP_API_KEY")

    model: str | None = Field(default=None, alias="OPENSCIENTIST_MODEL")
    model_context_tokens: int | None = Field(
        default=None,
        alias="OPENSCIENTIST_MODEL_CONTEXT_TOKENS",
        description=(
            "Override the model's usable context window (tokens) used to budget "
            "prompt size. When unset, the window is probed (Ollama, vLLM, llama.cpp) or "
            "looked up for known API models, falling back to a conservative default."
        ),
    )
    anthropic_chat_model: str | None = Field(
        default=None,
        alias="ANTHROPIC_CHAT_MODEL",
        description=(
            "Model used for in-page chat. Falls back to OPENSCIENTIST_MODEL if unset. "
            "Useful when the discovery model rejects chat-style prompts under its "
            "Usage Policy enforcement (e.g. Claude Opus 4.6 on Foundry). Operators "
            "can set this to a smaller, less-restrictive deployment such as Haiku."
        ),
    )
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

    aws_region: str | None = Field(default=None, alias="AWS_REGION")
    aws_access_key_id: str | None = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str | None = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_profile: str | None = Field(default=None, alias="AWS_PROFILE")
    aws_bearer_token_bedrock: str | None = Field(default=None, alias="AWS_BEARER_TOKEN_BEDROCK")

    anthropic_vertex_project_id: str | None = Field(
        default=None, alias="ANTHROPIC_VERTEX_PROJECT_ID"
    )
    google_application_credentials: str | None = Field(
        default=None, alias="GOOGLE_APPLICATION_CREDENTIALS"
    )
    # Host path for GCP credentials, mounted into agent containers
    gcp_credentials_host_path: str | None = Field(default=None, alias="GCP_CREDENTIALS_HOST_PATH")
    # Host path to the codex CLI auth file. The runner copies it into the
    # per-job CODEX_HOME so the Codex backend can authenticate via ChatGPT
    # login when no API key is set.
    codex_auth_host_path: str | None = Field(default=None, alias="CODEX_AUTH_HOST_PATH")
    # Host omp coding-agent dir (the agent.db vault). Provisioned into each job
    # so the omp harness can use omp's stored logins (e.g. a ChatGPT subscription).
    omp_auth_host_path: str | None = Field(default=None, alias="OMP_AUTH_HOST_PATH")
    gcp_billing_account_id: str | None = Field(default=None, alias="GCP_BILLING_ACCOUNT_ID")
    cloud_ml_region: str | None = Field(default=None, alias="CLOUD_ML_REGION")
    vertex_region_claude_4_5_sonnet: str | None = Field(
        default=None, alias="VERTEX_REGION_CLAUDE_4_5_SONNET"
    )
    vertex_region_claude_4_5_haiku: str | None = Field(
        default=None, alias="VERTEX_REGION_CLAUDE_4_5_HAIKU"
    )

    anthropic_foundry_resource: str | None = Field(default=None, alias="ANTHROPIC_FOUNDRY_RESOURCE")
    anthropic_foundry_base_url: str | None = Field(default=None, alias="ANTHROPIC_FOUNDRY_BASE_URL")
    anthropic_foundry_api_key: str | None = Field(default=None, alias="ANTHROPIC_FOUNDRY_API_KEY")
    # Azure Cost Management (enables billing data for the foundry provider)
    azure_subscription_id: str | None = Field(default=None, alias="AZURE_SUBSCRIPTION_ID")
    azure_resource_group: str | None = Field(default=None, alias="AZURE_RESOURCE_GROUP")
    # Azure AD service principal (for the Cost Management API)
    azure_tenant_id: str | None = Field(default=None, alias="AZURE_TENANT_ID")
    azure_client_id: str | None = Field(default=None, alias="AZURE_CLIENT_ID")
    azure_client_secret: str | None = Field(default=None, alias="AZURE_CLIENT_SECRET")

    @model_validator(mode="after")
    def require_provider_selected(self) -> "ProviderSettings":
        """There is no default provider. An unset ``OPENSCIENTIST_PROVIDER`` is
        an error so the running configuration is always explicit rather than
        silently falling back to one vendor.
        """
        if not self.provider_id:
            raise ValueError(
                "OPENSCIENTIST_PROVIDER is not set and there is no default "
                "provider. Set OPENSCIENTIST_PROVIDER to one of: anthropic, "
                "cborg, vertex, bedrock, foundry, openai, azure-openai, ollama, vllm, llamacpp."
            )
        return self

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> "ProviderSettings":
        """Delegate config validation to the active provider. Missing
        credentials warn (warn-only so settings always construct) and a
        model-name/provider mismatch raises. ``provider_class`` is imported
        lazily to avoid a settings -> providers import cycle.
        """
        from openscientist.providers import provider_class

        try:
            provider_cls = provider_class(self.provider_id)
        except ValueError:
            logger.warning(
                "Provider config: Unknown provider %r. Valid options: anthropic, "
                "cborg, vertex, bedrock, foundry, openai, azure-openai, ollama, vllm, llamacpp.",
                self.provider_id,
            )
            return self

        for warning in provider_cls.required_config_errors(self):
            logger.warning("Provider config: %s", warning)

        model_error = provider_cls.validate_model_format(self.model)
        if model_error:
            raise ValueError(model_error)
        return self

    @staticmethod
    def _set_env_if_present(env_vars: dict[str, str], key: str, value: str | None) -> None:
        if value:
            env_vars[key] = value

    def _apply_model_env_vars(self, env_vars: dict[str, str]) -> None:
        self._set_env_if_present(env_vars, "OPENSCIENTIST_MODEL", self.model)
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

    def get_container_env_vars(
        self,
        gcp_credentials_container_path: str | None = None,
    ) -> dict[str, str]:
        """Environment variables for the agent container. Backend-agnostic keys
        plus the active provider's own env (auth + routing flags).

        The provider builds its own contribution via ``Provider.container_env``,
        resolved by class. No instantiation, so this stays usable with partial
        config. ``provider_class`` is imported lazily to avoid a settings ->
        providers import cycle.
        """
        from openscientist.providers import provider_class

        env_vars: dict[str, str] = {
            "OPENSCIENTIST_PROVIDER": self.provider_id,
            "OPENSCIENTIST_HARNESS": self.harness,
        }
        self._apply_model_env_vars(env_vars)
        try:
            provider_cls = provider_class(self.provider_id)
        except ValueError:
            return env_vars
        env_vars.update(
            provider_cls.container_env(
                self, gcp_credentials_container_path=gcp_credentials_container_path
            )
        )
        return env_vars


class DatabaseSettings(BaseSettings):
    """Database configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(alias="DATABASE_URL")

    # Login for admin sessions, which bypass RLS. The web process requires it.
    # Agent containers load these settings without it.
    admin_database_url: str | None = Field(default=None, alias="ADMIN_DATABASE_URL")

    sql_echo: bool = Field(default=False, alias="SQL_ECHO")

    # Seconds each connect waits for an unreachable database. The job runner
    # sets it for agent containers, while the web fails fast.
    connect_retry_seconds: float = Field(
        default=0, ge=0, alias="OPENSCIENTIST_DB_CONNECT_RETRY_SECONDS"
    )

    @property
    def effective_database_url(self) -> str:
        return self.database_url

    def required_admin_database_url(self) -> str:
        """The admin login URL, for a process that opens admin sessions."""
        if not self.admin_database_url:
            raise ValueError(
                "ADMIN_DATABASE_URL is not set. Admin sessions need the openscientist_admin "
                "login and do not fall back to DATABASE_URL."
            )
        return self.admin_database_url


class MigrationSettings(BaseSettings):
    """Logins of the migration step (Alembic and the PubMed loader). The web never
    loads these, so it holds no owner password."""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.owner"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    owner_database_url: str | None = Field(default=None, alias="OWNER_DATABASE_URL")
    # Wins over a password in OWNER_DATABASE_URL, so .env.owner is its only source.
    postgres_owner_password: SecretStr | None = Field(default=None, alias="POSTGRES_OWNER_PASSWORD")
    # Password the migrations give openscientist_admin.
    postgres_admin_password: SecretStr | None = Field(default=None, alias="POSTGRES_ADMIN_PASSWORD")

    def required_owner_database_url(self) -> str:
        """The openscientist_owner URL, with POSTGRES_OWNER_PASSWORD filled in."""
        if not self.owner_database_url:
            raise ValueError(
                "OWNER_DATABASE_URL is not set. Migrations and the PubMed loader log in "
                "as openscientist_owner."
            )
        url = make_url(self.owner_database_url)
        if self.postgres_owner_password is not None:
            password = self.postgres_owner_password.get_secret_value()
            if password:
                url = url.set(password=password)
        return url.render_as_string(hide_password=False)


class AuthSettings(BaseSettings):
    """Authentication configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_url: str = Field(default="http://localhost:8080", alias="APP_URL")
    session_duration_days: int = Field(default=30, alias="SESSION_DURATION_DAYS")

    # Derived from OPENSCIENTIST_SECRET_KEY (populated by Settings.derive_secrets)
    storage_secret: str = Field(default="")
    token_encryption_key: str | None = Field(default=None)

    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")

    github_client_id: str | None = Field(default=None, alias="GITHUB_CLIENT_ID")
    github_client_secret: str | None = Field(default=None, alias="GITHUB_CLIENT_SECRET")

    orcid_client_id: str | None = Field(default=None, alias="ORCID_CLIENT_ID")
    orcid_client_secret: str | None = Field(default=None, alias="ORCID_CLIENT_SECRET")

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
        return bool(self.google_client_id or self.github_client_id or self.orcid_client_id)

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

    # Provider-agnostic budget limits
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

    executor_image: str = Field(
        default="openscientist-executor:latest", alias="OPENSCIENTIST_EXECUTOR_IMAGE"
    )
    agent_image: str = Field(
        default="openscientist-agent:latest",
        alias="OPENSCIENTIST_AGENT_IMAGE",
        description=(
            "Docker image tag used to spawn per-job agent containers. Override to "
            "point staging at separately-tagged images (e.g. openscientist-agent:staging) "
            "without colliding with the prod :latest image."
        ),
    )
    executor_memory: str = Field(default="2g", alias="OPENSCIENTIST_EXECUTOR_MEMORY")
    executor_cpu: float = Field(default=0.5, alias="OPENSCIENTIST_EXECUTOR_CPU")
    executor_timeout: int = Field(default=120, alias="OPENSCIENTIST_EXECUTOR_TIMEOUT")

    agent_memory: str = Field(default="8g", alias="OPENSCIENTIST_AGENT_MEMORY")
    agent_cpu: float = Field(default=2.0, alias="OPENSCIENTIST_AGENT_CPU")
    agent_network: str | None = Field(
        default=None,
        alias="OPENSCIENTIST_AGENT_NETWORK",
        description="Docker network for agent containers. Auto-detected from the web server container if not set.",
    )
    agent_platform: str | None = Field(
        default=None,
        alias="OPENSCIENTIST_AGENT_PLATFORM",
        description="Docker platform for agent containers (e.g. linux/amd64). "
        "Set to linux/amd64 on Apple Silicon to run x86_64 Phenix via Rosetta.",
    )
    agent_timeout: int = Field(
        default=4 * 3600,
        alias="OPENSCIENTIST_AGENT_TIMEOUT",
        description="Wall-clock seconds before a running job container is failed as timed out. "
        "Raise it for slow local models (e.g. large open-weight models on Ollama) that need "
        "more than the 4-hour default to finish all iterations.",
    )

    # Host path mapping for sibling container volume mounts. When the main
    # container runs inside Docker and spawns siblings, paths are translated
    # from container paths to host paths.
    container_app_dir: str = Field(default="/app", alias="OPENSCIENTIST_CONTAINER_APP_DIR")
    host_project_dir: str | None = Field(
        default=None,
        alias="OPENSCIENTIST_HOST_PROJECT_DIR",
        description="Host path for project directory. Required when using agent containers.",
    )
    llm_proxy_port: int = Field(
        default=8081,
        alias="OPENSCIENTIST_LLM_PROXY_PORT",
        description="Port of the web's LLM key-replacement proxy, which agent containers call.",
    )
    exec_broker_port: int = Field(
        default=8082,
        alias="OPENSCIENTIST_EXEC_BROKER_PORT",
        description="Port of the web's execution broker, which agent containers call.",
    )


class PhenixSettings(BaseSettings):
    """Phenix structural biology tools configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    phenix_path: str | None = Field(default=None, alias="PHENIX_PATH")
    phenix_host_path: str | None = Field(default=None, alias="PHENIX_HOST_PATH")

    @staticmethod
    def _validate_absolute_path(
        value: str | None,
        *,
        env_name: str,
        example: str,
    ) -> str | None:
        """Validate a configured filesystem path without requiring existence."""
        if value is None or value == "":
            return None

        if not value.startswith("/"):
            raise ValueError(
                f"{env_name} must be an absolute path (starting with '/'), got: '{value}'\n"
                f"  Example: {env_name}={example}"
            )

        if ".." in value:
            raise ValueError(f"{env_name} must not contain path traversal (..): '{value}'")

        return value

    @field_validator("phenix_path")
    @classmethod
    def validate_phenix_path(cls, v: str | None) -> str | None:
        """Validate PHENIX_PATH format if set.

        Format only. Existence is checked by the ``is_available`` property.
        """
        return cls._validate_absolute_path(
            v,
            env_name="PHENIX_PATH",
            example="/opt/phenix-2.0-5936",
        )

    @field_validator("phenix_host_path")
    @classmethod
    def validate_phenix_host_path(cls, v: str | None) -> str | None:
        """Validate PHENIX_HOST_PATH format if set."""
        return cls._validate_absolute_path(
            v,
            env_name="PHENIX_HOST_PATH",
            example="/home/user/phenix-2.0-5936",
        )

    @property
    def is_available(self) -> bool:
        """Check if Phenix is configured and available.

        Checks actual existence on the filesystem, complementing the format
        validation. Expects the Phenix 2.x install layout (``bin/phenix.about``).
        """
        if not self.phenix_path:
            return False
        if not os.path.isdir(self.phenix_path):
            return False
        about = os.path.join(self.phenix_path, "bin", "phenix.about")
        return os.path.exists(about)


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


class AirgapSettings(BaseSettings):
    """Master switch for air-gapped features."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = Field(
        default=False,
        alias="OPENSCIENTIST_AIRGAPPED",
        description="Enable air-gapped mode: local corpus, offline executor, and egress firewall.",
    )


class Settings(BaseSettings):
    """Root settings class with all configuration sections."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Master secret. All auth secrets derive from this via HMAC-SHA256.
    secret_key: str = Field(alias="OPENSCIENTIST_SECRET_KEY")

    port: int = Field(default=8080, alias="PORT")
    max_concurrent_jobs: int = Field(default=1, alias="OPENSCIENTIST_MAX_CONCURRENT_JOBS")
    # Hours of slot use the queue weighs each owner's queued jobs by
    scheduler_window_hours: float = Field(
        default=24, gt=0, alias="OPENSCIENTIST_SCHEDULER_WINDOW_HOURS"
    )
    base_url: str = Field(
        default="http://localhost:8080",
        alias="OPENSCIENTIST_BASE_URL",
        description="Base URL for OpenScientist (used in notifications and share links)",
    )

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
    airgap: AirgapSettings = Field(default_factory=AirgapSettings)

    @model_validator(mode="after")
    def derive_secrets(self) -> "Settings":
        """Derive auth secrets from the master OPENSCIENTIST_SECRET_KEY via HMAC-SHA256."""
        key = self.secret_key.encode()
        self.auth.storage_secret = hmac.new(key, b"storage_secret", hashlib.sha256).hexdigest()
        self.auth.token_encryption_key = hmac.new(
            key, b"token_encryption_key", hashlib.sha256
        ).hexdigest()
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Validate settings from the environment and return the cached singleton."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear the settings cache (useful for testing)."""
    get_settings.cache_clear()
