"""Tests for providers factory and base provider."""

import os
from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from openscientist.exceptions import ProviderError
from openscientist.providers import get_provider
from openscientist.providers.anthropic import AnthropicProvider
from openscientist.providers.base import BaseProvider, CostInfo
from openscientist.providers.bedrock import BedrockProvider
from openscientist.providers.cborg import CborgProvider
from openscientist.providers.foundry import FoundryProvider
from openscientist.providers.vertex import VertexProvider
from openscientist.settings import clear_settings_cache

# ─── Concrete stub for testing BaseProvider ───────────────────────────


class StubProvider(BaseProvider):
    """Minimal concrete provider for testing BaseProvider logic."""

    def __init__(
        self,
        cost_info: CostInfo | None = None,
        required_errors: list[str] | None = None,
        optional_warnings: list[str] | None = None,
    ):
        self._cost_info = cost_info
        self._required_errors = required_errors or []
        self._optional_warnings = optional_warnings or []
        super().__init__()

    def _validate_required_config(self) -> list[str]:
        return self._required_errors

    def _validate_optional_config(self) -> list[str]:
        return self._optional_warnings

    def setup_environment(self) -> None:
        pass

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        _ = lookback_hours
        if self._cost_info is None:
            raise ProviderError("No cost info configured")
        return self._cost_info

    @property
    def name(self) -> str:
        return "StubProvider"

    async def send_message(
        self,
        messages: list[dict[str, str]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> str:
        raise NotImplementedError("StubProvider does not send messages")

    async def send_message_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        raise NotImplementedError("StubProvider does not send messages")


# ─── Tests ────────────────────────────────────────────────────────────


class TestGetProvider:
    """Tests for the provider factory function."""

    def setup_method(self):
        """Clear settings cache before each test."""
        clear_settings_cache()

    def teardown_method(self):
        """Clear settings cache after each test."""
        clear_settings_cache()

    @patch.dict(
        os.environ,
        {
            "CLAUDE_PROVIDER": "cborg",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
        },
    )
    def test_cborg_provider(self):
        provider = get_provider()
        assert provider.name.lower() == "cborg"

    def test_vertex_provider(self, tmp_path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds_file),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            provider = get_provider()
            assert "vertex" in provider.name.lower()

    @patch.dict(
        os.environ,
        {
            "CLAUDE_PROVIDER": "bedrock",
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test-key",
            "AWS_SECRET_ACCESS_KEY": "test-secret",
        },
    )
    def test_bedrock_provider(self):
        """Bedrock provider initializes with valid AWS config."""
        provider = get_provider()
        assert "bedrock" in provider.name.lower()

    @patch.dict(os.environ, {"CLAUDE_PROVIDER": "codex"})
    def test_codex_provider_raises_not_implemented(self):
        """Codex is a stub — validation always fails."""
        with pytest.raises(ValueError, match="not yet implemented"):
            get_provider()

    @patch.dict(os.environ, {"CLAUDE_PROVIDER": "unknown_provider"})
    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            get_provider()

    def test_defaults_to_anthropic(self, monkeypatch, tmp_path):
        """Without CLAUDE_PROVIDER, defaults to anthropic (which may fail validation)."""
        # Change to temp dir to avoid .env file
        monkeypatch.chdir(tmp_path)

        # Clear environment of provider-related vars
        for key in list(os.environ.keys()):
            if key.startswith(("CLAUDE_", "ANTHROPIC_", "AWS_", "GOOGLE_", "GCP_", "VERTEX_")):
                monkeypatch.delenv(key, raising=False)

        # No ANTHROPIC_API_KEY → AnthropicProvider.__init__ raises.
        with pytest.raises(ValueError, match=r"Anthropic|ANTHROPIC_API_KEY"):
            get_provider()


class TestBaseProviderInit:
    """Tests for BaseProvider initialisation and validation."""

    def test_valid_config_no_errors(self):
        provider = StubProvider()
        assert provider.name == "StubProvider"

    def test_required_config_errors_raise(self):
        with pytest.raises(ValueError, match="configuration errors"):
            StubProvider(required_errors=["TOKEN is missing"])


class TestCostInfo:
    """Tests for CostInfo dataclass."""

    def test_basic_creation(self):
        info = CostInfo(
            provider_name="test",
            total_spend_usd=100.0,
            recent_spend_usd=10.0,
            recent_period_hours=24,
        )
        assert info.provider_name == "test"
        assert info.total_spend_usd == 100.0
        assert info.budget_limit_usd is None

    def test_optional_fields(self):
        info = CostInfo(
            provider_name="test",
            total_spend_usd=50.0,
            recent_spend_usd=5.0,
            recent_period_hours=24,
            budget_limit_usd=200.0,
            budget_remaining_usd=150.0,
            data_lag_note="Data current as of 6:35 AM",
        )
        assert info.budget_remaining_usd == 150.0
        assert info.data_lag_note == "Data current as of 6:35 AM"


class TestCheckBudgetLimits:
    """Tests for BaseProvider.check_budget_limits()."""

    def _make_provider(self, **cost_kwargs) -> StubProvider:
        cost = CostInfo(
            provider_name="stub",
            recent_period_hours=24,
            **cost_kwargs,
        )
        return StubProvider(cost_info=cost)

    def test_under_budget_can_proceed(self):
        provider = self._make_provider(total_spend_usd=10.0, recent_spend_usd=5.0)
        with patch.dict(
            os.environ,
            {
                "MAX_PROJECT_SPEND_TOTAL_USD": "1000",
                "MAX_PROJECT_SPEND_24H_USD": "100",
            },
        ):
            result = provider.check_budget_limits()
        assert result["can_proceed"] is True
        assert result["errors"] == []

    def test_total_spend_exceeded_blocks(self):
        provider = self._make_provider(total_spend_usd=1001.0, recent_spend_usd=5.0)
        with patch.dict(os.environ, {"MAX_PROJECT_SPEND_TOTAL_USD": "1000"}):
            result = provider.check_budget_limits()
        assert result["can_proceed"] is False
        assert any("Total spend" in e for e in result["errors"])

    def test_recent_spend_exceeded_blocks(self):
        provider = self._make_provider(total_spend_usd=10.0, recent_spend_usd=200.0)
        with patch.dict(os.environ, {"MAX_PROJECT_SPEND_24H_USD": "100"}):
            result = provider.check_budget_limits()
        assert result["can_proceed"] is False
        assert any("Last 24h" in e for e in result["errors"])

    def test_warning_threshold_warns_but_allows(self):
        provider = self._make_provider(total_spend_usd=10.0, recent_spend_usd=80.0)
        with patch.dict(
            os.environ,
            {
                "MAX_PROJECT_SPEND_24H_USD": "100",
                "WARN_PROJECT_SPEND_24H_USD": "75",
            },
        ):
            clear_settings_cache()
            result = provider.check_budget_limits()
        assert result["can_proceed"] is True
        assert any("approaching" in w for w in result["warnings"])

    def test_provider_budget_exhausted_blocks(self):
        provider = self._make_provider(
            total_spend_usd=10.0,
            recent_spend_usd=5.0,
            budget_limit_usd=100.0,
            budget_remaining_usd=0.0,
        )
        result = provider.check_budget_limits()
        assert result["can_proceed"] is False
        assert any("exhausted" in e for e in result["errors"])

    def test_provider_budget_low_warns(self):
        provider = self._make_provider(
            total_spend_usd=10.0,
            recent_spend_usd=5.0,
            budget_limit_usd=100.0,
            budget_remaining_usd=5.0,
        )
        result = provider.check_budget_limits()
        assert result["can_proceed"] is True
        assert any("budget low" in w for w in result["warnings"])

    def test_cost_info_unavailable_warns_but_allows(self):
        provider = self._make_provider(
            total_spend_usd=None,
            recent_spend_usd=None,
            data_lag_note="Billing data delayed",
        )
        result = provider.check_budget_limits()
        assert result["can_proceed"] is True
        assert any("unavailable" in w for w in result["warnings"])

    def test_cost_fetch_failure_warns_but_allows(self):
        provider = StubProvider(cost_info=None)  # get_cost_info will raise
        result = provider.check_budget_limits()
        assert result["can_proceed"] is True
        assert any("Could not check" in w for w in result["warnings"])


# ─── check_provider_config ────────────────────────────────────────────


class TestCheckProviderConfig:
    """Tests for check_provider_config()."""

    def setup_method(self):
        clear_settings_cache()

    def teardown_method(self):
        clear_settings_cache()

    @patch.dict(os.environ, {"SIMULATE_PROVIDER_ERROR": "true"})
    def test_simulate_error(self):
        from openscientist.providers import check_provider_config

        ok, name, errors = check_provider_config()
        assert ok is False
        assert name == "anthropic"
        assert len(errors) == 2
        assert any("ANTHROPIC_API_KEY" in e for e in errors)

    @patch.dict(os.environ, {"CLAUDE_PROVIDER": "totally_bogus"})
    def test_unknown_provider(self):
        from openscientist.providers import check_provider_config

        ok, name, errors = check_provider_config()
        assert ok is False
        assert name == "totally_bogus"
        assert any("Unknown provider" in e for e in errors)

    @patch.dict(
        os.environ,
        {
            "CLAUDE_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-test-valid",
        },
    )
    def test_valid_anthropic(self):
        from openscientist.providers import check_provider_config

        ok, name, errors = check_provider_config()
        assert ok is True
        assert name == "anthropic"
        assert errors == []

    @patch.dict(
        os.environ,
        {
            "CLAUDE_PROVIDER": "cborg",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
        },
    )
    def test_valid_cborg(self):
        from openscientist.providers import check_provider_config

        ok, name, errors = check_provider_config()
        assert ok is True
        assert name == "cborg"
        assert errors == []


class TestGetProviderAllNames:
    """Tests for get_provider() with each valid provider name."""

    def setup_method(self):
        clear_settings_cache()

    def teardown_method(self):
        clear_settings_cache()

    @patch.dict(
        os.environ,
        {"CLAUDE_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-test"},
    )
    def test_anthropic(self):
        provider = get_provider()
        assert provider.name == "Anthropic"

    @patch.dict(
        os.environ,
        {
            "CLAUDE_PROVIDER": "cborg",
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
        },
    )
    def test_cborg(self):
        provider = get_provider()
        assert provider.name == "CBORG"

    @patch.dict(
        os.environ,
        {
            "CLAUDE_PROVIDER": "bedrock",
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "secret",
        },
    )
    def test_bedrock(self):
        provider = get_provider()
        assert provider.name == "AWS Bedrock"

    def test_vertex(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            provider = get_provider()
            assert provider.name == "Vertex AI"

    @patch.dict(os.environ, {"CLAUDE_PROVIDER": "unknown_xyz"})
    def test_unknown_raises_with_valid_options(self):
        with pytest.raises(ValueError, match="Valid options"):
            get_provider()


class TestProviderEnvironmentSwitching:
    """Regression tests for provider switching in the same process."""

    @staticmethod
    def _foundry_settings() -> MagicMock:
        settings = MagicMock()
        settings.provider.anthropic_foundry_resource = "lab-foundry"
        settings.provider.anthropic_foundry_base_url = None
        settings.provider.anthropic_foundry_api_key = "foundry-key"
        settings.provider.anthropic_default_sonnet_model = "claude-sonnet-4-5"
        settings.provider.anthropic_default_haiku_model = None
        settings.provider.anthropic_default_opus_model = None
        return settings

    @staticmethod
    def _anthropic_settings() -> MagicMock:
        settings = MagicMock()
        settings.provider.anthropic_api_key = "sk-ant-test-key"
        settings.provider.claude_code_oauth_token = None
        settings.provider.anthropic_model = "claude-sonnet-4-6"
        return settings

    @staticmethod
    def _cborg_settings() -> MagicMock:
        settings = MagicMock()
        settings.provider.anthropic_auth_token = "cborg-token"
        settings.provider.anthropic_base_url = "https://api.cborg.lbl.gov"
        settings.provider.anthropic_model = "claude-sonnet-4-6"
        return settings

    @staticmethod
    def _vertex_settings() -> MagicMock:
        settings = MagicMock()
        settings.provider.anthropic_vertex_project_id = "vertex-project"
        settings.provider.google_application_credentials = "/tmp/vertex-creds.json"
        settings.provider.gcp_billing_account_id = "012345-ABCDEF"
        settings.provider.cloud_ml_region = "us-east5"
        settings.provider.anthropic_model = "claude-sonnet-4-5@20250929"
        settings.provider.vertex_region_claude_4_5_sonnet = None
        settings.provider.vertex_region_claude_4_5_haiku = None
        return settings

    @staticmethod
    def _bedrock_settings() -> MagicMock:
        settings = MagicMock()
        settings.provider.aws_region = "us-east-1"
        settings.provider.aws_access_key_id = "AKIA..."
        settings.provider.aws_secret_access_key = "secret"
        settings.provider.aws_profile = None
        settings.provider.aws_bearer_token_bedrock = None
        settings.provider.anthropic_model = "claude-sonnet-4-5"
        settings.provider.anthropic_small_fast_model = "claude-haiku-4-5"
        return settings

    @pytest.mark.parametrize(
        (
            "provider_cls",
            "settings_patch_target",
            "settings_factory_name",
            "expects_bedrock_flag",
        ),
        [
            (
                AnthropicProvider,
                "openscientist.providers.anthropic.get_settings",
                "_anthropic_settings",
                False,
            ),
            (
                CborgProvider,
                "openscientist.providers.cborg.get_settings",
                "_cborg_settings",
                False,
            ),
            (
                VertexProvider,
                "openscientist.providers.vertex.get_settings",
                "_vertex_settings",
                False,
            ),
            (
                BedrockProvider,
                "openscientist.providers.bedrock.get_settings",
                "_bedrock_settings",
                True,
            ),
        ],
    )
    def test_switching_away_from_foundry_clears_foundry_flag(
        self,
        provider_cls: type[AnthropicProvider | CborgProvider | VertexProvider | BedrockProvider],
        settings_patch_target: str,
        settings_factory_name: str,
        expects_bedrock_flag: bool,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "openscientist.providers.foundry.get_settings",
                return_value=self._foundry_settings(),
            ):
                FoundryProvider().setup_environment()

            assert os.environ.get("CLAUDE_CODE_USE_FOUNDRY") == "1"

            settings_factory = getattr(self, settings_factory_name)
            patchers = [patch(settings_patch_target, return_value=settings_factory())]
            if provider_cls is VertexProvider:
                patchers.append(
                    patch("openscientist.providers.vertex.os.path.exists", return_value=True)
                )

            with ExitStack() as stack:
                for patcher in patchers:
                    stack.enter_context(patcher)
                provider_cls().setup_environment()

            assert "CLAUDE_CODE_USE_FOUNDRY" not in os.environ
            if expects_bedrock_flag:
                assert os.environ.get("CLAUDE_CODE_USE_BEDROCK") == "1"
