"""Billing and cost tracking panel."""

import logging

from nicegui import ui
from sqlalchemy import func, select

from openscientist.admin_settings import current_admin_settings
from openscientist.budget import check_budget, project_spend, warning_limit_reached
from openscientist.database.models import CostRecord, User
from openscientist.database.session import get_admin_session
from openscientist.providers import get_provider
from openscientist.webapp_components.components.common import (
    render_empty_state,
    render_stat_badges,
)

logger = logging.getLogger(__name__)


async def render_billing_panel() -> None:
    """Render the billing panel (for use as an admin subtab)."""
    with ui.column().classes("w-full gap-6"):
        await _render_db_cost_section()
        await _render_user_cost_section()
        await _render_project_budget_section()


def _iteration_label(record: CostRecord) -> str:
    """Show the iteration, operation and any estimated provenance."""
    if record.operation_type.endswith("_estimated"):
        operation = record.operation_type.removesuffix("_estimated")
        prefix = f"{record.iteration} " if record.iteration is not None else ""
        return f"{prefix}{operation} estimated"
    if record.iteration is not None:
        return str(record.iteration)
    return record.operation_type if record.operation_type in ("report", "chat") else ""


async def _render_db_cost_section() -> None:
    """Query CostRecord and display per-job cost breakdown."""
    async with get_admin_session() as session:
        records = await session.execute(select(CostRecord).order_by(CostRecord.created_at.desc()))
        cost_records = records.scalars().all()

    if not cost_records:
        render_empty_state(
            "No cost records yet. Costs are recorded per model call by the LLM proxy."
        )
        return

    total_input = sum(r.input_tokens for r in cost_records)
    total_output = sum(r.output_tokens for r in cost_records)
    total_cache_read = sum(r.cache_read_tokens for r in cost_records)
    total_cache_write = sum(r.cache_write_tokens for r in cost_records)
    total_reasoning = sum(r.reasoning_tokens for r in cost_records)
    total_cost = sum(r.cost_usd for r in cost_records)

    # Every bucket that cost_usd prices is shown, otherwise the cost per token
    # reads as nonsense: cached reads usually dominate an agentic run.
    render_stat_badges(
        [
            ("Total Cost", f"${total_cost:.4f}", "green"),
            ("Input Tokens", f"{total_input:,}", "blue"),
            ("Output Tokens", f"{total_output:,}", "orange"),
            ("Cache Read", f"{total_cache_read:,}", "purple"),
            ("Cache Write", f"{total_cache_write:,}", "teal"),
            ("Reasoning", f"{total_reasoning:,}", "pink"),
        ]
    )

    # Short headers and the dense prop keep the table inside its card at narrow
    # widths; the table scrolls horizontally inside the card if it still overflows.
    columns = [
        {"name": "job_id", "label": "Job", "field": "job_id", "align": "left"},
        {"name": "iteration", "label": "It.", "field": "iteration", "align": "right"},
        {"name": "model", "label": "Model", "field": "model", "align": "left"},
        {"name": "provider", "label": "Prov.", "field": "provider", "align": "left"},
        {"name": "input_tokens", "label": "In", "field": "input_tokens", "align": "right"},
        {"name": "output_tokens", "label": "Out", "field": "output_tokens", "align": "right"},
        {
            "name": "cache_read_tokens",
            "label": "Cache R",
            "field": "cache_read_tokens",
            "align": "right",
        },
        {
            "name": "cache_write_tokens",
            "label": "Cache W",
            "field": "cache_write_tokens",
            "align": "right",
        },
        {
            "name": "reasoning_tokens",
            "label": "Reason",
            "field": "reasoning_tokens",
            "align": "right",
        },
        {"name": "cost_usd", "label": "Cost", "field": "cost_usd", "align": "right"},
        {"name": "created_at", "label": "Date", "field": "created_at", "align": "left"},
    ]
    rows = [
        {
            "id": str(r.id),
            "job_id": "..." + str(r.job_id)[-8:] if r.job_id is not None else "Deleted job",
            "iteration": _iteration_label(r),
            "model": r.model,
            "provider": r.provider,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cache_write_tokens": r.cache_write_tokens,
            "reasoning_tokens": r.reasoning_tokens,
            "cost_usd": f"${r.cost_usd:.4f}",
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for r in cost_records
    ]
    ui.table(columns=columns, rows=rows, row_key="id").props("dense").classes("w-full")


async def _render_user_cost_section() -> None:
    """Show each user's lifetime expenditure, including deleted jobs."""
    async with get_admin_session() as session:
        total = func.sum(CostRecord.cost_usd)
        result = await session.execute(
            select(CostRecord.user_id, User.name, User.email, total)
            .outerjoin(User, User.id == CostRecord.user_id)
            .group_by(CostRecord.user_id, User.name, User.email)
            .order_by(total.desc())
        )
        rows = [
            {
                "user_id": str(user_id) if user_id is not None else "",
                "user": f"{name} ({email})"
                if user_id is not None
                else "Deleted user or unassigned",
                "cost_usd": f"${cost:.4f}",
            }
            for user_id, name, email, cost in result
        ]
    if rows:
        ui.label("Lifetime Expenditure by User").classes("text-h6")
        ui.table(
            columns=[
                {"name": "user", "label": "User", "field": "user", "align": "left"},
                {"name": "cost_usd", "label": "Cost", "field": "cost_usd", "align": "right"},
            ],
            rows=rows,
            row_key="user_id",
        ).props("dense").classes("w-full")


async def _render_project_budget_section() -> None:
    """The project spend and the verdict the budget enforcement acts on."""
    with ui.card().classes("w-full max-w-4xl os-density-card"):
        ui.label("Project Budget").classes("text-h6 mb-2")

        try:
            provider = get_provider()
            # The spend figures and the verdict the enforcement uses, from the
            # same check_budget the moments of spend call.
            spend = await project_spend()
            verdict = await check_budget(provider_id=provider.id, include_unpriced=False)
            admin = await current_admin_settings()

            with ui.row().classes("w-full gap-4 mb-2 flex-wrap"):
                # Total spend
                with ui.card().classes("flex-1 min-w-40 os-density-card"):
                    ui.label(f"${spend.total:.2f}").classes("text-2xl text-primary")
                    ui.label("Total Spend").classes("text-subtitle2 text-grey")

                # Last 24h
                with ui.card().classes("flex-1 min-w-40 os-density-card"):
                    ui.label(f"${spend.last_24h:.2f}").classes("text-2xl")
                    ui.label("Last 24 Hours").classes("text-subtitle2 text-grey")

            # Provider info
            with ui.card().classes("w-full bg-gray-50 os-density-card"):
                ui.label("Provider Information").classes("text-subtitle2 font-bold mb-2")
                provider_name = (
                    spend.billed.provider_name
                    if spend.billed is not None
                    else provider.display_name
                )
                ui.label(f"Provider: {provider_name}").classes("text-sm")
                if spend.billed is not None and spend.billed.data_lag_note:
                    ui.label(spend.billed.data_lag_note).classes("text-sm text-grey-6")

            if not verdict.ok:
                with ui.card().classes("w-full bg-red-50 border border-red-300 mt-4 p-4"):
                    ui.label("Budget Limit Reached").classes(
                        "text-subtitle2 font-bold text-red-800 mb-2"
                    )
                    ui.label(f"⚠️ {verdict.message}").classes("text-sm text-red-600")
            elif warning_limit_reached(
                spend.last_24h,
                admin.warn_project_spend_24h_usd,
                admin.max_project_spend_24h_usd,
            ):
                warn = admin.warn_project_spend_24h_usd
                with ui.card().classes("w-full bg-yellow-50 border border-yellow-300 mt-4 p-4"):
                    ui.label("Budget Warning").classes(
                        "text-subtitle2 font-bold text-yellow-800 mb-2"
                    )
                    if warn is not None:
                        ui.label(
                            f"⚠️ Last 24 h spend ${spend.last_24h:.2f} is at or above the "
                            f"warning threshold ${warn:.2f}"
                        ).classes("text-sm text-yellow-600")

        except Exception as e:
            logger.warning("Cost tracking unavailable: %s", e)
            with ui.card().classes("w-full bg-yellow-50 border border-yellow-300 p-4"):
                ui.label("Cost Tracking Unavailable").classes(
                    "text-subtitle2 font-bold text-yellow-800 mb-2"
                )
                ui.label("Check provider configuration in .env").classes("text-sm text-gray-600")
