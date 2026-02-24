"""Tests for cost_tracker module."""

import os
from unittest.mock import MagicMock, patch

import pytest

from shandy.cost_tracker import (
    BudgetExceededError,
    check_budget_before_job,
    estimate_total_cost,
    get_budget_info,
    get_cborg_spend,
    get_cost_per_iteration,
    track_job_cost,
)
from shandy.settings import clear_settings_cache


class TestGetCostPerIteration:
    """Tests for average cost per iteration calculation."""

    def test_zero_iterations_returns_zero(self):
        assert get_cost_per_iteration(5.0, 0) == 0.0

    def test_one_iteration(self):
        assert get_cost_per_iteration(2.0, 1) == 2.0

    def test_multiple_iterations(self):
        assert get_cost_per_iteration(10.0, 5) == pytest.approx(2.0)

    def test_fractional_cost(self):
        assert get_cost_per_iteration(1.0, 3) == pytest.approx(1 / 3)


class TestEstimateTotalCost:
    """Tests for total cost estimation."""

    def test_zero_iterations_returns_zero(self):
        assert estimate_total_cost(5.0, 0, 20) == 0.0

    def test_linear_extrapolation(self):
        # 2.0 spent over 5 iterations = 0.4/iter → 20 iterations = 8.0
        assert estimate_total_cost(2.0, 5, 20) == pytest.approx(8.0)

    def test_single_iteration(self):
        assert estimate_total_cost(0.5, 1, 10) == pytest.approx(5.0)


class TestTrackJobCost:
    """Tests for job cost tracking and budget enforcement."""

    @patch("shandy.cost_tracker.get_cborg_spend")
    def test_returns_job_cost(self, mock_spend):
        mock_spend.return_value = 15.0  # current
        cost = track_job_cost("j1", start_spend=10.0)
        assert cost == pytest.approx(5.0)

    @patch("shandy.cost_tracker.get_cborg_spend")
    def test_exceeds_budget_raises(self, mock_spend):
        mock_spend.return_value = 100.0
        with (
            patch.dict(os.environ, {"MAX_JOB_COST_USD": "10.0"}),
            pytest.raises(
                BudgetExceededError,
                match="exceeds limit",
            ),
        ):
            track_job_cost("j1", start_spend=0.0)

    @patch("shandy.cost_tracker.get_cborg_spend")
    def test_within_budget_no_error(self, mock_spend):
        mock_spend.return_value = 12.0
        with patch.dict(os.environ, {"MAX_JOB_COST_USD": "50.0"}):
            cost = track_job_cost("j1", start_spend=10.0)
            assert cost == pytest.approx(2.0)


class TestGetCborgSpend:
    """Tests for CBORG API spend query."""

    def test_no_token_raises(self):
        # Mock get_settings so .env file values don't leak in
        mock_settings = MagicMock()
        mock_settings.provider.anthropic_auth_token = None
        with (
            patch("shandy.cost_tracker.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="ANTHROPIC_AUTH_TOKEN",
            ),
        ):
            get_cborg_spend()

    @patch("shandy.cost_tracker.requests.get")
    def test_returns_spend(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "test-token",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"info": {"spend": 42.5}}
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            result = get_cborg_spend()
            assert result == 42.5
            mock_get.assert_called_once()


class TestGetBudgetInfo:
    """Tests for budget info retrieval."""

    @patch("shandy.cost_tracker.requests.get")
    def test_returns_budget_info(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
                "MAX_JOB_COST_USD": "15.0",
                "APP_MAX_BUDGET_USD": "500.0",
            },
        ):
            clear_settings_cache()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "info": {
                    "spend": 100.0,
                    "max_budget": 200.0,
                    "expires": "2026-12-31",
                }
            }
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            info = get_budget_info()
            assert info["current_spend"] == 100.0
            assert info["cborg_max_budget"] == 200.0
            assert info["budget_remaining"] == pytest.approx(100.0)
            assert info["app_max_job_cost"] == 15.0
            assert info["app_max_total_budget"] == 500.0
            assert info["key_expires"] == "2026-12-31"

    @patch("shandy.cost_tracker.requests.get")
    def test_no_cborg_budget(self, mock_get):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "cborg",
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.cborg.lbl.gov",
            },
        ):
            clear_settings_cache()
            mock_resp = MagicMock()
            mock_resp.json.return_value = {
                "info": {"spend": 50.0, "max_budget": None, "expires": "2027-01-01"}
            }
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            info = get_budget_info()
            assert info["cborg_max_budget"] is None
            assert info["budget_remaining"] is None


class TestCheckBudgetBeforeJob:
    """Tests for pre-job budget check."""

    @patch("shandy.cost_tracker.get_budget_info")
    def test_sufficient_budget_passes(self, mock_info):
        mock_info.return_value = {
            "cborg_max_budget": 200.0,
            "budget_remaining": 100.0,
            "current_spend": 100.0,
            "app_max_total_budget": 500.0,
        }
        check_budget_before_job(estimated_cost=5.0)  # should not raise

    @patch("shandy.cost_tracker.get_budget_info")
    def test_insufficient_cborg_budget_raises(self, mock_info):
        mock_info.return_value = {
            "cborg_max_budget": 100.0,
            "budget_remaining": 2.0,
            "current_spend": 98.0,
            "app_max_total_budget": 500.0,
        }
        with pytest.raises(ValueError, match="Insufficient CBORG budget"):
            check_budget_before_job(estimated_cost=5.0)

    @patch("shandy.cost_tracker.get_budget_info")
    def test_exceeds_app_budget_raises(self, mock_info):
        mock_info.return_value = {
            "cborg_max_budget": None,
            "budget_remaining": None,
            "current_spend": 498.0,
            "app_max_total_budget": 500.0,
        }
        with pytest.raises(ValueError, match="exceed app budget"):
            check_budget_before_job(estimated_cost=5.0)
