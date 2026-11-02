"""Tests for CBORG provider."""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from openscientist.providers.base import AirgapEgress
from openscientist.providers.cborg import CborgProvider
from openscientist.settings import clear_settings_cache


class TestCborgProviderValidation:
    """Tests for CBORG provider configuration validation."""

    def test_valid_config(self):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "test-token",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            provider = CborgProvider()
            assert provider.display_name == "CBORG"

    def test_missing_token_raises(self):
        # Mock get_settings so .env file values don't leak in
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_auth_token = None
        mock_settings.provider.anthropic_base_url = "https://api.cborg.lbl.gov"
        mock_settings.provider.model = "claude-sonnet-4-6"
        with (
            patch("openscientist.providers.cborg.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="ANTHROPIC_AUTH_TOKEN",
            ),
        ):
            CborgProvider()

    def test_missing_base_url_raises(self):
        # Mock get_settings so .env file values don't leak in
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_auth_token = "tok"
        mock_settings.provider.anthropic_base_url = None
        mock_settings.provider.model = "claude-sonnet-4-6"
        with (
            patch("openscientist.providers.cborg.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="ANTHROPIC_BASE_URL",
            ),
        ):
            CborgProvider()


class TestCborgSetupEnvironment:
    """Tests for CBORG environment setup."""

    def test_setup_does_not_raise(self):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            provider = CborgProvider()
            provider.setup_environment()  # should just log


class TestCborgGetCostInfo:
    """Tests for CBORG cost info retrieval."""

    @patch("openscientist.providers.cborg.requests.get")
    def test_returns_cost_info(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            # Mock /key/info
            key_resp = MagicMock()
            key_resp.json.return_value = {
                "info": {"spend": 50.0, "max_budget": 200.0, "expires": "2026-12-31"}
            }
            key_resp.raise_for_status = MagicMock()

            # Mock /user/daily/activity
            activity_resp = MagicMock()
            activity_resp.json.return_value = {"data": [{"spend": 5.0}, {"spend": 3.0}]}
            activity_resp.raise_for_status = MagicMock()

            mock_get.side_effect = [key_resp, activity_resp]

            provider = CborgProvider()
            cost = provider.get_cost_info(lookback_hours=24)

            assert cost.provider_name == "CBORG"
            assert cost.total_spend_usd == 50.0
            assert cost.recent_spend_usd == 8.0
            assert cost.budget_limit_usd == 200.0
            assert cost.budget_remaining_usd == 150.0
            assert cost.key_expires == "2026-12-31"

    @patch("openscientist.providers.cborg.requests.get")
    def test_activity_failure_falls_back(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            # Mock /key/info succeeds
            key_resp = MagicMock()
            key_resp.json.return_value = {
                "info": {"spend": 50.0, "max_budget": None, "expires": "2026-12-31"}
            }
            key_resp.raise_for_status = MagicMock()

            # Mock /user/daily/activity fails
            activity_resp = MagicMock()
            activity_resp.raise_for_status.side_effect = requests.HTTPError("API error")

            mock_get.side_effect = [key_resp, activity_resp]

            provider = CborgProvider()
            cost = provider.get_cost_info()

            assert cost.total_spend_usd == 50.0
            assert cost.recent_spend_usd is None
            assert "unavailable" in (cost.data_lag_note or "").lower()

    @patch("openscientist.providers.cborg.requests.get")
    def test_no_max_budget(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            key_resp = MagicMock()
            key_resp.json.return_value = {
                "info": {"spend": 50.0, "max_budget": None, "expires": "2026-12-31"}
            }
            key_resp.raise_for_status = MagicMock()

            activity_resp = MagicMock()
            activity_resp.json.return_value = {"data": []}
            activity_resp.raise_for_status = MagicMock()

            mock_get.side_effect = [key_resp, activity_resp]

            provider = CborgProvider()
            cost = provider.get_cost_info()

            assert cost.budget_limit_usd is None
            assert cost.budget_remaining_usd is None


def _mock_settings(
    *,
    token: str | None = "test-token",
    base_url: str | None = "https://api.cborg.lbl.gov",
    model: str | None = "claude-sonnet-4-6",
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.provider.anthropic_auth_token = token
    mock_settings.provider.anthropic_base_url = base_url
    mock_settings.provider.model = model
    return mock_settings


class TestCborgClaudeCompatible:
    """Tests for the ClaudeCompatible family methods."""

    def test_id_is_cborg(self) -> None:
        with patch("openscientist.providers.cborg.get_settings", return_value=_mock_settings()):
            assert CborgProvider().id == "cborg"

    def test_display_name_is_cborg(self) -> None:
        with patch("openscientist.providers.cborg.get_settings", return_value=_mock_settings()):
            assert CborgProvider().display_name == "CBORG"

    def test_is_claude_compatible_and_provider(self) -> None:
        from openscientist.providers.base import (
            ClaudeCompatible,
            CodexCompatible,
            Provider,
        )

        with patch("openscientist.providers.cborg.get_settings", return_value=_mock_settings()):
            provider = CborgProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ClaudeCompatible)
        assert not isinstance(provider, CodexCompatible)

    def test_validate_required_config_ok(self) -> None:
        assert CborgProvider.required_config_errors(_mock_settings().provider) == []

    def test_validate_required_config_errors_when_both_missing(self) -> None:
        errors = CborgProvider.required_config_errors(
            _mock_settings(token=None, base_url=None).provider
        )
        assert len(errors) == 2
        assert any("ANTHROPIC_AUTH_TOKEN" in e for e in errors)
        assert any("ANTHROPIC_BASE_URL" in e for e in errors)

    def test_claude_sdk_env_returns_token_and_base_url(self) -> None:
        settings = _mock_settings(token="tok-123", base_url="https://api.cborg.lbl.gov")
        with patch("openscientist.providers.cborg.get_settings", return_value=settings):
            provider = CborgProvider()
            assert provider.claude_sdk_env() == {
                "ANTHROPIC_AUTH_TOKEN": "tok-123",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            }

    def test_claude_sdk_env_omits_unset_keys(self) -> None:
        # Construct valid, then re-point settings so base_url is unset.
        with patch("openscientist.providers.cborg.get_settings", return_value=_mock_settings()):
            provider = CborgProvider()
        with patch(
            "openscientist.providers.cborg.get_settings",
            return_value=_mock_settings(token="only-token", base_url=None),
        ):
            env = provider.claude_sdk_env()
        assert env == {"ANTHROPIC_AUTH_TOKEN": "only-token"}

    def test_claude_model_name_uses_configured_model(self) -> None:
        settings = _mock_settings(model="claude-custom-model")
        with (
            patch("openscientist.providers.cborg.get_settings", return_value=settings),
            patch("openscientist.providers.base.get_settings", return_value=settings),
        ):
            assert CborgProvider().claude_model_name() == "claude-custom-model"


def test_airgap_posture_is_unsupported_without_credentials() -> None:
    """Without an endpoint and a token the provider cannot be routed through the proxy."""
    posture = CborgProvider.airgap_posture(
        SimpleNamespace(anthropic_base_url=None, anthropic_auth_token=None)  # type: ignore[arg-type]
    )
    assert posture.mode is AirgapEgress.UNSUPPORTED
