"""Tests for CBORG provider."""

import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from shandy.providers.cborg import CborgProvider


class TestCborgProviderValidation:
    """Tests for CBORG provider configuration validation."""

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_AUTH_TOKEN": "test-token",
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
        },
    )
    def test_valid_config(self):
        provider = CborgProvider()
        assert provider.name == "CBORG"

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_token_raises(self):
        with pytest.raises(ValueError, match="ANTHROPIC_AUTH_TOKEN"):
            CborgProvider()

    @patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "tok"}, clear=True)
    def test_missing_base_url_raises(self):
        with pytest.raises(ValueError, match="ANTHROPIC_BASE_URL"):
            CborgProvider()


class TestCborgSetupEnvironment:
    """Tests for CBORG environment setup."""

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
        },
    )
    def test_setup_does_not_raise(self):
        provider = CborgProvider()
        provider.setup_environment()  # should just log


class TestCborgGetCostInfo:
    """Tests for CBORG cost info retrieval."""

    @patch("shandy.providers.cborg.requests.get")
    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
        },
    )
    def test_returns_cost_info(self, mock_get):
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
    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
        },
    )
    def test_activity_failure_falls_back(self, mock_get):
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
    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_AUTH_TOKEN": "tok",
            "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
        },
    )
    def test_no_max_budget(self, mock_get):
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
