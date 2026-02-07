"""
Cost tracking via CBORG API for SHANDY.

Monitors API spending and enforces budget limits.
"""

import os
from typing import Any, Dict

import requests

from shandy.exceptions import BudgetExceededError


def get_cborg_spend() -> float:
    """
    Query CBORG API for current spend.

    Returns:
        Current spend in USD

    Raises:
        requests.RequestException: If API call fails
    """
    api_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
    if not api_token:
        raise ValueError("ANTHROPIC_AUTH_TOKEN not set in environment")

    response = requests.get(
        "https://api.cborg.lbl.gov/key/info",
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=10,
    )
    response.raise_for_status()

    return float(response.json()["info"]["spend"])


def get_budget_info() -> Dict[str, Any]:
    """
    Get budget information from CBORG and application settings.

    Returns:
        Dictionary with budget information:
        - current_spend: Current CBORG spend (float)
        - cborg_max_budget: CBORG budget limit if set (float|None)
        - budget_remaining: CBORG budget remaining (float|None)
        - app_max_job_cost: Per-job limit from .env (float)
        - app_max_total_budget: Total app budget from .env (float)
        - key_expires: API key expiration date (str)
    """
    api_token = os.getenv("ANTHROPIC_AUTH_TOKEN")
    if not api_token:
        raise ValueError("ANTHROPIC_AUTH_TOKEN not set in environment")

    response = requests.get(
        "https://api.cborg.lbl.gov/key/info",
        headers={"Authorization": f"Bearer {api_token}"},
        timeout=10,
    )
    response.raise_for_status()

    info = response.json()["info"]
    current_spend = info["spend"]
    cborg_budget = info.get("max_budget")  # May be None

    # Application-level limits from .env
    app_max_job = float(os.getenv("MAX_JOB_COST_USD", "10.0"))
    app_max_total = float(os.getenv("APP_MAX_BUDGET_USD", "1000.0"))

    return {
        "current_spend": current_spend,
        "cborg_max_budget": cborg_budget,
        "budget_remaining": cborg_budget - current_spend if cborg_budget else None,
        "app_max_job_cost": app_max_job,
        "app_max_total_budget": app_max_total,
        "key_expires": info["expires"],
    }


def check_budget_before_job(estimated_cost: float = 5.0) -> None:
    """
    Check if we have enough budget to run a job.

    Args:
        estimated_cost: Estimated job cost in USD

    Raises:
        ValueError: If insufficient budget
    """
    budget_info = get_budget_info()

    # Check CBORG budget (if set)
    if budget_info["cborg_max_budget"]:
        if budget_info["budget_remaining"] < estimated_cost:
            raise ValueError(
                f"Insufficient CBORG budget: "
                f"${budget_info['budget_remaining']:.2f} remaining, "
                f"need ~${estimated_cost}"
            )

    # Check application-level limit
    if budget_info["current_spend"] + estimated_cost > budget_info["app_max_total_budget"]:
        raise ValueError(f"Would exceed app budget limit of ${budget_info['app_max_total_budget']}")


def track_job_cost(job_id: str, start_spend: float) -> float:
    """
    Update job metadata with current cost.

    Args:
        job_id: Job identifier
        start_spend: Spend at job start

    Returns:
        Current job cost in USD

    Raises:
        BudgetExceededError: If job exceeds per-job limit
    """
    current_spend = get_cborg_spend()
    job_cost = current_spend - start_spend

    # Check if exceeding per-job limit
    max_job_cost = float(os.getenv("MAX_JOB_COST_USD", "10.0"))
    if job_cost > max_job_cost:
        raise BudgetExceededError(
            f"Job {job_id} cost ${job_cost:.2f} exceeds limit ${max_job_cost}"
        )

    return job_cost


def get_cost_per_iteration(job_cost: float, iteration: int) -> float:
    """
    Calculate average cost per iteration.

    Args:
        job_cost: Total job cost so far
        iteration: Current iteration number

    Returns:
        Average cost per iteration
    """
    if iteration == 0:
        return 0.0
    return job_cost / iteration


def estimate_total_cost(job_cost: float, iteration: int, max_iterations: int) -> float:
    """
    Estimate total job cost based on current pace.

    Args:
        job_cost: Cost so far
        iteration: Current iteration
        max_iterations: Maximum iterations

    Returns:
        Estimated total cost
    """
    if iteration == 0:
        return 0.0

    cost_per_iter = job_cost / iteration
    return cost_per_iter * max_iterations
