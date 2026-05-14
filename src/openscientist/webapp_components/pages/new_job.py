"""New job submission page."""

import logging
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nicegui import ui

from openscientist.auth import can_current_user_start_jobs, get_current_user_id, require_auth
from openscientist.providers import check_provider_config
from openscientist.webapp_components.ui_components import (
    render_config_error_banner,
    render_navigator,
    render_pending_approval_notice,
)
from openscientist.webapp_components.utils.session import (
    add_uploaded_file,
    clear_uploaded_files,
    get_uploaded_files,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemplateFieldConfig:
    """Configuration for a template-specific form field."""

    key: str
    label: str
    placeholder: str
    required: bool = True


@dataclass(frozen=True)
class JobTemplateConfig:
    """Configuration for a guided job template."""

    label: str
    guidance: str
    skill_hints: tuple[str, ...]
    analysis_requirements: tuple[str, ...]
    report_guidance: tuple[str, ...]
    fields: tuple[TemplateFieldConfig, ...] = ()


FREEFORM_TEMPLATE_ID = "freeform"
JOB_TEMPLATES: dict[str, JobTemplateConfig] = {
    "go_gene_set_enrichment": JobTemplateConfig(
        label="GO Gene Set Enrichment",
        guidance=(
            "Provide a target gene set and a background gene set. The agent will prioritize "
            "deterministic GO enrichment statistics before interpretation."
        ),
        skill_hints=("execute_code", "plotting"),
        analysis_requirements=(
            "Require over-representation testing against an explicit background set.",
            "Use GO enrichment methods (not KEGG) unless user asks otherwise.",
            "Do not make biological claims without statistical evidence from the enrichment run.",
        ),
        report_guidance=(
            "Include adjusted p-values, effect sizes, and top enriched GO terms in a table.",
            "Highlight how background-set choice influences interpretation.",
        ),
        fields=(
            TemplateFieldConfig(
                key="target_gene_set",
                label="Target Gene Set",
                placeholder="Paste gene symbols or IDs for the gene set of interest.",
            ),
            TemplateFieldConfig(
                key="background_gene_set",
                label="Background Gene Set",
                placeholder="Paste gene symbols or IDs for the background universe.",
            ),
            TemplateFieldConfig(
                key="organism",
                label="Organism (optional)",
                placeholder="e.g., Homo sapiens",
                required=False,
            ),
        ),
    ),
    "variant_interpretation": JobTemplateConfig(
        label="Variant Interpretation",
        guidance=(
            "Provide variants and context. The agent will prioritize deterministic annotation "
            "steps before narrative interpretation."
        ),
        skill_hints=("execute_code", "literature_search"),
        analysis_requirements=(
            "Start with explicit variant annotation and evidence aggregation before conclusions.",
            "Separate known evidence from speculative interpretation.",
        ),
        report_guidance=(
            "Provide a per-variant evidence table with source, confidence, and limitations.",
            "List recommended follow-up validation experiments.",
        ),
        fields=(
            TemplateFieldConfig(
                key="variants",
                label="Variants",
                placeholder="List variants (e.g., BRCA1 c.68_69delAG, rsID, genomic coordinates).",
            ),
            TemplateFieldConfig(
                key="phenotype_context",
                label="Phenotype / Clinical Context",
                placeholder="Describe phenotype, cohort, or disease context.",
            ),
        ),
    ),
}


def _build_template_description(
    template_id: str,
    template_inputs: dict[str, str],
) -> str | None:
    """Build structured template guidance that is injected into job description."""
    template = JOB_TEMPLATES.get(template_id)
    if template is None:
        return None

    lines = [
        f"Template selected: {template.label}",
        "",
        "Template guidance:",
        template.guidance,
        "",
        "Preferred skills/tools:",
        *[f"- {skill}" for skill in template.skill_hints],
        "",
        "Analysis requirements:",
        *[f"- {requirement}" for requirement in template.analysis_requirements],
        "",
        "Reporting guidance:",
        *[f"- {guidance}" for guidance in template.report_guidance],
    ]

    field_lines: list[str] = []
    missing_required: list[str] = []
    for field in template.fields:
        value = template_inputs.get(field.key, "").strip()
        if value:
            field_lines.append(f"- {field.label}: {value}")
        elif field.required:
            missing_required.append(field.label)
            field_lines.append(f"- {field.label}: [MISSING]")

    if field_lines:
        lines.extend(["", "Template inputs:", *field_lines])

    if missing_required:
        lines.extend(
            [
                "",
                "Required template inputs missing:",
                *[f"- {label}" for label in missing_required],
                "Ask the scientist to provide missing required inputs before final interpretation.",
            ]
        )

    return "\n".join(lines)


def _collect_template_input_values(template_inputs: dict[str, Any]) -> dict[str, str]:
    """Return trimmed values from template input widgets."""
    values: dict[str, str] = {}
    for key, field in template_inputs.items():
        raw_value = getattr(field, "value", "")
        values[key] = str(raw_value or "").strip()
    return values


def _build_upload_session_id(user_id: str | None, client: object) -> str:
    """Build an upload-session key scoped to user and websocket client."""
    effective_user_id = user_id or "anonymous"
    client_id = str(getattr(client, "id", id(client)))
    return f"{effective_user_id}:{client_id}"


def _persist_uploaded_files(session_id: str) -> list[Path]:
    """Return temp file paths for all uploaded files in the session."""
    return [uploaded_file["path"] for uploaded_file in get_uploaded_files(session_id)]


def _notify_creation_error(error: Exception) -> None:
    """Show user-friendly notification for job creation failures."""
    error_msg = str(error).lower()
    if "authentication" in error_msg or "api key" in error_msg:
        ui.notify(
            "Authentication error. Please contact your administrator to check API credentials.",
            type="negative",
        )
        return
    if "event loop" in error_msg:
        ui.notify("Internal server error. Please try again or contact support.", type="negative")
        return
    ui.notify("Error creating job. Please try again or contact support.", type="negative")


def _submit_job(
    *,
    job_manager: Any,
    user_can_start_jobs: bool,
    session_id: str,
    research_question: ui.textarea,
    max_iterations: ui.number,
    use_hypotheses: ui.switch,
    coinvestigate_mode: ui.switch,
    template_id: ui.select,
    template_inputs: dict[str, Any],
) -> None:
    """Validate input and create a new discovery job."""
    if not user_can_start_jobs:
        ui.notify("Your account is pending administrator approval.", type="warning")
        return

    question = research_question.value.strip()
    if not question:
        ui.notify("Please enter a research question", type="negative")
        return

    current_user_id = get_current_user_id()
    if not current_user_id:
        ui.notify("Authentication required. Please log in again.", type="negative")
        ui.navigate.to("/login")
        return

    job_id = str(uuid.uuid4())
    data_files = _persist_uploaded_files(session_id)
    mode = "coinvestigate" if coinvestigate_mode.value else "autonomous"
    selected_template = str(template_id.value or FREEFORM_TEMPLATE_ID)
    description = _build_template_description(
        selected_template,
        _collect_template_input_values(template_inputs),
    )

    try:
        job_manager.create_job(
            job_id=job_id,
            research_question=question,
            data_files=data_files,
            max_iterations=int(max_iterations.value),
            use_hypotheses=use_hypotheses.value,
            auto_start=True,
            investigation_mode=mode,
            owner_id=current_user_id,
            description=description,
        )
        ui.notify(f"Job {job_id} created and started!", type="positive")
        clear_uploaded_files(session_id)
        ui.navigate.to(f"/job/{job_id}")
    except Exception as exc:
        logger.error("Error creating job: %s", exc, exc_info=True)
        _notify_creation_error(exc)


async def _handle_upload(e: Any, session_id: str) -> None:
    """Stream upload directly to a temp file and record its path in session state."""
    try:
        name = e.file.name
        temp_path = Path(tempfile.mkdtemp()) / name
        await e.file.save(temp_path)
        add_uploaded_file(session_id, name, temp_path)
        ui.notify(f"Uploaded: {name}", type="positive")
        logger.info("Successfully uploaded %s (%d bytes)", name, e.file.size())
    except (ValueError, OSError) as exc:
        logger.error("Upload failed: %s", exc, exc_info=True)
        ui.notify(f"Upload failed: {exc}", type="negative")


@ui.page("/new")
@require_auth
def new_job_page() -> None:
    """Job submission form."""
    from openscientist import web_app

    job_manager = web_app.get_job_manager()
    user_can_start_jobs = can_current_user_start_jobs()
    user_id = get_current_user_id()
    client = ui.context.client
    session_id = _build_upload_session_id(user_id, client)
    client.on_disconnect(lambda: clear_uploaded_files(session_id))

    is_configured, provider_name, config_errors = check_provider_config()
    render_navigator(active_page="new", show_new_job=is_configured)

    if not user_can_start_jobs:
        render_pending_approval_notice()
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs")).props(
            "outline color=primary"
        ).classes("mt-4")
        return

    if not is_configured:
        render_config_error_banner(provider_name, config_errors, show_back_button=True)
        return

    async def on_upload(event: Any) -> None:
        await _handle_upload(event, session_id)

    with ui.card().classes("w-full max-w-2xl mx-auto mt-8"):
        ui.label("Submit Discovery Job").classes("text-h5 mb-4")

        research_question = ui.textarea(
            label="Research Question",
            placeholder="e.g., What metabolic pathways are affected by hypothermia?",
            validation={"Too short": lambda value: len(value) >= 10},
        ).classes("w-full")

        template_fields: dict[str, Any] = {}
        template_options = {FREEFORM_TEMPLATE_ID: "Freeform (No Template)"}
        template_options.update({key: config.label for key, config in JOB_TEMPLATES.items()})
        template_id = ui.select(
            options=template_options,
            value=FREEFORM_TEMPLATE_ID,
            label="Guided Template (Optional)",
        ).classes("w-full")
        template_container = ui.column().classes("w-full")

        def render_template_inputs() -> None:
            """Render template guidance and input fields for the selected template."""
            selected = str(template_id.value or FREEFORM_TEMPLATE_ID)
            template = JOB_TEMPLATES.get(selected)
            template_fields.clear()
            template_container.clear()
            if template is None:
                return
            with template_container:
                ui.label(template.guidance).classes("text-sm text-gray-700")
                for field in template.fields:
                    field_label = field.label if field.required else f"{field.label} (optional)"
                    template_fields[field.key] = ui.textarea(
                        label=field_label,
                        placeholder=field.placeholder,
                    ).classes("w-full")

        template_id.on("update:model-value", lambda _: render_template_inputs())
        render_template_inputs()

        ui.upload(
            label="Upload Data Files (Optional - Tabular, Structures, Sequences, Images)",
            multiple=True,
            auto_upload=True,
            on_upload=on_upload,
        ).classes("w-full")
        ui.label("Maximum file size: 500 MB per file").classes("text-caption text-grey-6")

        max_iterations = ui.number(
            label="Max Iterations",
            value=10,
            min=2,
            max=100,
            step=1,
        ).classes("w-full")

        ui.separator().classes("my-4")
        use_hypotheses = ui.switch("Hypothesis Generation", value=False)
        ui.label(
            "Track scientific hypotheses across iterations — propose, test, and confirm/reject them."
        ).classes("text-sm text-gray-700 mt-1")

        ui.separator().classes("my-4")
        coinvestigate_mode = ui.switch("Coinvestigate Mode", value=False)
        ui.label(
            "Requires your active participation. After each iteration, I will pause to receive your feedback."
        ).classes("text-sm text-gray-700 mt-1")
        ui.label(
            "Requires you to stay near your computer. Auto-continues after 15 min if you don't respond."
        ).classes("text-xs text-orange-700")

        ui.button(
            "Start Discovery",
            on_click=lambda: _submit_job(
                job_manager=job_manager,
                user_can_start_jobs=user_can_start_jobs,
                session_id=session_id,
                research_question=research_question,
                max_iterations=max_iterations,
                use_hypotheses=use_hypotheses,
                coinvestigate_mode=coinvestigate_mode,
                template_id=template_id,
                template_inputs=template_fields,
            ),
        ).classes("w-full mt-4")
