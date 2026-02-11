"""Tests for providers factory and base provider."""

import os
from typing import List, Optional
from unittest.mock import patch

import pytest

from shandy.exceptions import ProviderError
from shandy.providers import get_provider
from shandy.providers.base import BaseProvider, CostInfo
from shandy.settings import clear_settings_cache

# ─── Concrete stub for testing BaseProvider ───────────────────────────


class StubProvider(BaseProvider):
    """Minimal concrete provider for testing BaseProvider logic."""

    def __init__(
        self,
        cost_info: Optional[CostInfo] = None,
        required_errors: Optional[List[str]] = None,
        optional_warnings: Optional[List[str]] = None,
    ):
        self._cost_info = cost_info
        self._required_errors = required_errors or []
        self._optional_warnings = optional_warnings or []
        super().__init__()

    def _validate_required_config(self) -> List[str]:
        return self._required_errors

    def _validate_optional_config(self) -> List[str]:
        return self._optional_warnings

    def setup_environment(self) -> None:
        pass

    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        if self._cost_info is None:
            raise ProviderError("No cost info configured")
        return self._cost_info

    @property
    def name(self) -> str:
        return "StubProvider"


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

    def test_defaults_to_cborg(self, monkeypatch, tmp_path):
        """Without CLAUDE_PROVIDER, defaults to cborg (which may fail validation)."""
        # Change to temp dir to avoid .env file
        monkeypatch.chdir(tmp_path)

        # Clear environment of provider-related vars
        for key in list(os.environ.keys()):
            if key.startswith(("CLAUDE_", "ANTHROPIC_", "AWS_", "GOOGLE_", "GCP_", "VERTEX_")):
                monkeypatch.delenv(key, raising=False)

        # We don't set ANTHROPIC_AUTH_TOKEN, so cborg validation will fail—
        # but the factory still routes to cborg.
        with pytest.raises(ValueError, match="cborg|ANTHROPIC_AUTH_TOKEN"):
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
