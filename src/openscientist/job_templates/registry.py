"""Guided job template registry.

Templates are a submit-time UX layer. ``resolve_template_submission`` turns a
selected template + structured inputs into the effective ``research_question``
and a ``description`` carrying the methodology guidance. Both are existing job
fields, so nothing downstream needs to know templates exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openscientist.job_templates.builtins import BUILTIN_TEMPLATES
from openscientist.job_templates.types import (
    JobTemplate,
    TemplateResolution,
    TemplateValidationError,
)

FREEFORM_TEMPLATE_ID = "freeform"
FREEFORM_DEFAULT_MAX_ITERATIONS = 10

_TEMPLATES: dict[str, JobTemplate] = {template.id: template for template in BUILTIN_TEMPLATES}


def normalize_template_id(template_id: str | None) -> str | None:
    """Normalize empty/freeform identifiers to None."""
    if template_id is None:
        return None
    normalized = template_id.strip()
    if not normalized or normalized == FREEFORM_TEMPLATE_ID:
        return None
    return normalized


def list_job_templates() -> list[JobTemplate]:
    """Return guided templates in display order."""
    return list(_TEMPLATES.values())


def get_job_template(template_id: str | None) -> JobTemplate | None:
    """Return a guided template, or None for freeform jobs."""
    normalized = normalize_template_id(template_id)
    if normalized is None:
        return None
    return _TEMPLATES.get(normalized)


def get_job_template_or_raise(template_id: str) -> JobTemplate:
    """Return a template or raise a validation error for unknown IDs."""
    template = get_job_template(template_id)
    if template is None:
        raise TemplateValidationError(f"Unknown job template: {template_id}.")
    return template


def default_max_iterations_for_template(template_id: str | None) -> int:
    """Return the suggested iteration count for a workflow selector value."""
    template = get_job_template(template_id)
    if template is None:
        return FREEFORM_DEFAULT_MAX_ITERATIONS
    return template.default_max_iterations


def workflow_options() -> dict[str, str]:
    """Return selector options for the new-job UI."""
    return {FREEFORM_TEMPLATE_ID: "Freeform"} | {
        template.id: template.name for template in list_job_templates()
    }


def build_template_research_question(
    template_id: str | None,
    template_inputs: Mapping[str, Any] | None,
) -> str:
    """Generate the default research question for a template (used for live preview)."""
    template = get_job_template(template_id)
    if template is None:
        return ""
    normalized_inputs = template.validate_inputs(template_inputs)
    return template.build_research_question(normalized_inputs)


def resolve_template_submission(
    *,
    template_id: str | None,
    template_inputs: Mapping[str, Any] | None,
    research_question: str | None,
) -> TemplateResolution:
    """Validate template inputs and produce the effective research question + guidance."""
    question = (research_question or "").strip()
    template = get_job_template(template_id)

    if template is None:
        if not question:
            raise TemplateValidationError("Please enter a research question.")
        return TemplateResolution(
            research_question=question,
            description=None,
            default_max_iterations=FREEFORM_DEFAULT_MAX_ITERATIONS,
        )

    normalized_inputs = template.validate_inputs(template_inputs)
    effective_question = question or template.build_research_question(normalized_inputs)
    if not effective_question:
        raise TemplateValidationError(f"{template.name} could not generate a research question.")

    return TemplateResolution(
        research_question=effective_question,
        description=template.build_guidance(normalized_inputs),
        default_max_iterations=template.default_max_iterations,
    )
