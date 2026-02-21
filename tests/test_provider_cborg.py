"""Tests for CBORG provider."""

import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from shandy.providers.cborg import CborgProvider
from shandy.settings import clear_settings_cache


class TestCborgProviderValidation:
    """Tests for CBORG provider configuration validation."""

    def test_valid_config(self):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "test-token",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            provider = CborgProvider()
            assert provider.name == "CBORG"

    def test_missing_token_raises(self):
        # Mock get_settings so .env file values don't leak in
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_auth_token = None
        mock_settings.provider.anthropic_base_url = "https://api.cborg.lbl.gov"
        mock_settings.provider.anthropic_model = "claude-sonnet-4-6"
        with patch("shandy.providers.cborg.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError, match="ANTHROPIC_AUTH_TOKEN"):
                CborgProvider()

    def test_missing_base_url_raises(self):
        # Mock get_settings so .env file values don't leak in
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_auth_token = "tok"
        mock_settings.provider.anthropic_base_url = None
        mock_settings.provider.anthropic_model = "claude-sonnet-4-6"
        with patch("shandy.providers.cborg.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError, match="ANTHROPIC_BASE_URL"):
                CborgProvider()


class TestCborgSetupEnvironment:
    """Tests for CBORG environment setup."""

    def test_setup_does_not_raise(self):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            provider = CborgProvider()
            provider.setup_environment()  # should just log


class TestCborgGetCostInfo:
    """Tests for CBORG cost info retrieval."""

    @patch("shandy.providers.cborg.requests.get")
    def test_returns_cost_info(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "cborg",
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

    @patch("shandy.providers.cborg.requests.get")
    def test_activity_failure_falls_back(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "cborg",
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
            assert cost.recent_spend_usd == 0.0  # Fallback

    @patch("shandy.providers.cborg.requests.get")
    def test_no_max_budget(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "cborg",
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
