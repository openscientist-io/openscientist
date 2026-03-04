"""Billing and cost tracking panel."""

import logging

from nicegui import ui
from sqlalchemy import select

from open_scientist.database.models import CostRecord
from open_scientist.database.session import get_admin_session
from open_scientist.providers import get_provider
from open_scientist.webapp_components.ui_components import render_empty_state, render_stat_badges

logger = logging.getLogger(__name__)


async def render_billing_panel() -> None:
    """Render the billing panel (for use as an admin subtab)."""
    with ui.column().classes("w-full gap-6"):
        await _render_db_cost_section()
        _render_provider_cost_section()


async def _render_db_cost_section() -> None:
    """Query CostRecord and display per-job cost breakdown."""
    async with get_admin_session() as session:
        records = await session.execute(select(CostRecord).order_by(CostRecord.created_at.desc()))
        cost_records = records.scalars().all()

    if not cost_records:
        render_empty_state("No cost records yet. Costs are recorded when jobs complete.")
        return

    total_input = sum(r.input_tokens for r in cost_records)
    total_output = sum(r.output_tokens for r in cost_records)
    total_cost = sum(r.cost_usd for r in cost_records)

    render_stat_badges(
        [
            ("Total Cost", f"${total_cost:.4f}", "green"),
            ("Input Tokens", f"{total_input:,}", "blue"),
            ("Output Tokens", f"{total_output:,}", "orange"),
        ]
    )

    columns = [
        {"name": "job_id", "label": "Job", "field": "job_id", "align": "left"},
        {"name": "model", "label": "Model", "field": "model", "align": "left"},
        {"name": "provider", "label": "Provider", "field": "provider", "align": "left"},
        {
            "name": "input_tokens",
            "label": "Input Tokens",
            "field": "input_tokens",
            "align": "right",
        },
        {
            "name": "output_tokens",
            "label": "Output Tokens",
            "field": "output_tokens",
            "align": "right",
        },
        {"name": "cost_usd", "label": "Cost (USD)", "field": "cost_usd", "align": "right"},
        {"name": "created_at", "label": "Date", "field": "created_at", "align": "left"},
    ]
    rows = [
        {
            "id": str(r.id),
            "job_id": str(r.job_id)[:8] + "...",
            "model": r.model,
            "provider": r.provider,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cost_usd": f"${r.cost_usd:.4f}",
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for r in cost_records
    ]
    ui.table(columns=columns, rows=rows, row_key="id").classes("w-full")


def _render_provider_cost_section() -> None:
    """Provider-level cost API."""
    with ui.card().classes("w-full max-w-4xl"):
        ui.label("Provider Cost API").classes("text-h6 mb-4")

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

        except Exception as e:
            logger.warning("Cost tracking unavailable: %s", e)
            with ui.card().classes("w-full bg-yellow-50 border border-yellow-300 p-4"):
                ui.label("Cost Tracking Unavailable").classes(
                    "text-subtitle2 font-bold text-yellow-800 mb-2"
                )
                ui.label("Check provider configuration in .env").classes("text-sm text-gray-600")
