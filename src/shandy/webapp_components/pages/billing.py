"""Billing and cost tracking page."""

import logging

from nicegui import ui

from shandy.auth import require_auth
from shandy.providers import get_provider
from shandy.webapp_components.ui_components import render_navigator


@ui.page("/billing")
@require_auth
def billing_page() -> None:
    """Billing and cost tracking page."""
    render_navigator(active_page="billing")

    with ui.card().classes("w-full max-w-4xl mx-auto mt-8"):
        ui.label("Project Costs").classes("text-h5 mb-4")

        try:
            provider = get_provider()
            cost_info = provider.get_cost_info(lookback_hours=24)
            budget_check = provider.check_budget_limits()

            with ui.row().classes("w-full gap-8 mb-4"):
                # Total spend
                with ui.card().classes("flex-1"):
                    total_spend_display = (
                        f"${cost_info.total_spend_usd:.2f}"
                        if cost_info.total_spend_usd is not None
                        else "N/A"
                    )
                    ui.label(total_spend_display).classes("text-h3 text-primary")
                    ui.label("Total Spend").classes("text-subtitle2 text-grey")

                # Last 24h
                with ui.card().classes("flex-1"):
                    recent_spend_display = (
                        f"${cost_info.recent_spend_usd:.2f}"
                        if cost_info.recent_spend_usd is not None
                        else "N/A"
                    )
                    ui.label(recent_spend_display).classes("text-h3")
                    ui.label(f"Last {cost_info.recent_period_hours} Hours").classes(
                        "text-subtitle2 text-grey"
                    )

                # Budget remaining (if available)
                if cost_info.budget_remaining_usd is not None:
                    with ui.card().classes("flex-1"):
                        remaining_color = (
                            "text-positive"
                            if cost_info.budget_remaining_usd > 10
                            else "text-warning"
                        )
                        ui.label(f"${cost_info.budget_remaining_usd:.2f}").classes(
                            f"text-h3 {remaining_color}"
                        )
                        ui.label("Budget Remaining").classes("text-subtitle2 text-grey")

            # Provider info
            with ui.card().classes("w-full bg-gray-50"):
                ui.label("Provider Information").classes("text-subtitle2 font-bold mb-2")
                ui.label(f"Provider: {cost_info.provider_name}").classes("text-sm")
                if cost_info.data_lag_note:
                    ui.label(cost_info.data_lag_note).classes("text-sm text-grey-6")

            # Budget warnings/errors
            if budget_check and budget_check.get("errors"):
                with ui.card().classes("w-full bg-red-50 border border-red-300 mt-4 p-4"):
                    ui.label("Budget Alerts").classes("text-subtitle2 font-bold text-red-800 mb-2")
                    for error in budget_check["errors"]:
                        ui.label(f"⚠️ {error}").classes("text-sm text-red-600")
            elif budget_check and budget_check.get("warnings"):
                with ui.card().classes("w-full bg-yellow-50 border border-yellow-300 mt-4 p-4"):
                    ui.label("Budget Warnings").classes(
                        "text-subtitle2 font-bold text-yellow-800 mb-2"
                    )
                    for warning in budget_check["warnings"]:
                        ui.label(f"⚠️ {warning}").classes("text-sm text-yellow-600")

        except (ValueError, OSError) as e:
            logging.getLogger(__name__).warning("Cost tracking unavailable: %s", e)
            with ui.card().classes("w-full bg-yellow-50 border border-yellow-300 p-4"):
                ui.label("Cost Tracking Unavailable").classes(
                    "text-subtitle2 font-bold text-yellow-800 mb-2"
                )
                ui.label("Check provider configuration in .env").classes("text-sm text-gray-600")
