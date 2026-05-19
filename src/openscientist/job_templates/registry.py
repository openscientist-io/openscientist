"""Guided job template registry."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from openscientist.job_templates.loader import load_job_templates_from_paths
from openscientist.job_templates.types import (
    JobTemplate,
    TemplateResolution,
    TemplateValidationError,
)

FREEFORM_TEMPLATE_ID = "freeform"
FREEFORM_DEFAULT_MAX_ITERATIONS = 10


def _load_registry() -> dict[str, JobTemplate]:
    return {template.id: template for template in load_job_templates_from_paths()}


_TEMPLATES: dict[str, JobTemplate] = _load_registry()


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


def parse_template_inputs(value: Any) -> dict[str, Any] | None:
    """Parse template input payloads from JSON, form strings, or dictionaries."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("template_inputs must be a JSON object.") from exc
        if parsed is None:
            return None
        if not isinstance(parsed, dict):
            raise ValueError("template_inputs must be a JSON object.")
        return parsed
    raise ValueError("template_inputs must be a JSON object.")


def resolve_template_submission(
    *,
    template_id: str | None,
    template_inputs: Mapping[str, Any] | None,
    research_question: str | None,
) -> TemplateResolution:
    """Validate template inputs and produce the effective research question."""
    normalized_template_id = normalize_template_id(template_id)
    question = (research_question or "").strip()

    if normalized_template_id is None:
        if not question:
            raise TemplateValidationError("Please enter a research question.")
        return TemplateResolution(
            template_id=None,
            template_version=None,
            template_inputs=None,
            research_question=question,
            agent_guidance=None,
        )

    template = get_job_template_or_raise(normalized_template_id)
    normalized_inputs = template.validate_inputs(template_inputs)
    effective_question = question or template.build_research_question(normalized_inputs)
    if not effective_question:
        raise TemplateValidationError(f"{template.name} could not generate a research question.")

    return TemplateResolution(
        template_id=template.id,
        template_version=template.version,
        template_inputs=normalized_inputs,
        research_question=effective_question,
        agent_guidance=template.build_agent_guidance(normalized_inputs),
    )


def build_template_agent_guidance(
    template_id: str | None,
    template_inputs: Mapping[str, Any] | None,
) -> str | None:
    """Build runtime guidance for a persisted template submission."""
    normalized_template_id = normalize_template_id(template_id)
    if normalized_template_id is None:
        return None
    template = get_job_template(normalized_template_id)
    if template is None:
        return None
    normalized_inputs = template.validate_inputs(template_inputs)
    return template.build_agent_guidance(normalized_inputs)


def build_template_research_question(
    template_id: str | None,
    template_inputs: Mapping[str, Any] | None,
) -> str:
    """Generate the default research question for a template."""
    normalized_template_id = normalize_template_id(template_id)
    if normalized_template_id is None:
        return ""
    template = get_job_template_or_raise(normalized_template_id)
    normalized_inputs = template.validate_inputs(template_inputs)
    return template.build_research_question(normalized_inputs)


def filter_skills_for_template(skills: Iterable[Any], template_id: str | None) -> list[Any]:
    """Return the skills that should be written for a selected template.

    Freeform jobs keep current behavior and receive all enabled skills. Guided
    jobs receive workflow skills, template-matched domain skills, and any
    template-bundled skills that are not already present from the database.
    """
    template = get_job_template(template_id)
    skill_list = list(skills)
    if template is None:
        return skill_list

    selected: list[Any] = []
    selected_keys: set[tuple[str, str]] = set()
    for skill in skill_list:
        if _skill_matches_template(skill, template):
            key = (str(getattr(skill, "category", "")), str(getattr(skill, "slug", "")))
            if key not in selected_keys:
                selected.append(skill)
                selected_keys.add(key)

    for skill in template.bundled_skills:
        key = (skill.category, skill.slug)
        if key not in selected_keys:
            selected.append(skill)
            selected_keys.add(key)

    return selected


def _skill_matches_template(skill: Any, template: JobTemplate) -> bool:
    category = str(getattr(skill, "category", "") or "")
    slug = str(getattr(skill, "slug", "") or "")
    name = str(getattr(skill, "name", "") or "").lower()
    tags = getattr(skill, "tags", []) or []
    tag_values = {str(tag).lower() for tag in tags}

    if category == "workflow":
        return True
    if category in template.skill_categories:
        return True
    if slug in template.skill_slugs:
        return True
    if f"{category}:{slug}" in template.skill_slugs:
        return True

    template_terms = set(template.skill_slugs) | set(template.skill_categories)
    normalized_terms = {term.split(":", 1)[-1].replace("-", " ") for term in template_terms}
    return bool(tag_values.intersection(template_terms)) or any(
        term in name for term in normalized_terms
    )
