"""Guided job template registry."""

from openscientist.job_templates.registry import (
    FREEFORM_DEFAULT_MAX_ITERATIONS,
    FREEFORM_TEMPLATE_ID,
    build_template_agent_guidance,
    build_template_research_question,
    default_max_iterations_for_template,
    filter_skills_for_template,
    get_job_template,
    get_job_template_or_raise,
    list_job_templates,
    normalize_template_id,
    parse_template_inputs,
    resolve_template_submission,
    workflow_options,
)
from openscientist.job_templates.types import (
    JobTemplate,
    TemplateField,
    TemplateFieldOption,
    TemplateResolution,
    TemplateValidationError,
)

__all__ = [
    "FREEFORM_DEFAULT_MAX_ITERATIONS",
    "FREEFORM_TEMPLATE_ID",
    "JobTemplate",
    "TemplateField",
    "TemplateFieldOption",
    "TemplateResolution",
    "TemplateValidationError",
    "build_template_agent_guidance",
    "build_template_research_question",
    "default_max_iterations_for_template",
    "filter_skills_for_template",
    "get_job_template",
    "get_job_template_or_raise",
    "list_job_templates",
    "normalize_template_id",
    "parse_template_inputs",
    "resolve_template_submission",
    "workflow_options",
]
