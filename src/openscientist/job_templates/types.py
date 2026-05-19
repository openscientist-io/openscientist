"""Types and validation helpers for guided job templates."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

TemplateFieldKind = Literal["text", "textarea", "select"]


class TemplateValidationError(ValueError):
    """Raised when a template submission is missing required scientific context."""


@dataclass(frozen=True)
class TemplateFieldOption:
    """Selectable value for a template field."""

    value: str
    label: str


@dataclass(frozen=True)
class TemplateField:
    """A field rendered by the new-job form and validated server-side."""

    key: str
    label: str
    kind: TemplateFieldKind = "text"
    required: bool = False
    placeholder: str = ""
    help_text: str = ""
    options: Sequence[TemplateFieldOption] = ()
    default: str | None = None
    rows: int = 3

    def option_labels(self) -> dict[str, str]:
        """Return a NiceGUI-friendly value-to-label mapping."""
        return {option.value: option.label for option in self.options}


@dataclass(frozen=True)
class TemplateSkill:
    """Skill document bundled with one or more guided job templates."""

    name: str
    category: str
    slug: str
    description: str
    content: str


@dataclass(frozen=True)
class JobTemplate:
    """Built-in guided workflow template."""

    id: str
    version: str
    name: str
    summary: str
    fields: Sequence[TemplateField]
    skill_slugs: Sequence[str]
    skill_categories: Sequence[str]
    methodology: Sequence[str]
    report_guidance: Sequence[str]
    visualization_guidance: Sequence[str]
    question_builder: Callable[[Mapping[str, Any]], str]
    default_max_iterations: int = 10
    bundled_skills: Sequence[TemplateSkill] = ()

    def validate_inputs(self, inputs: Mapping[str, Any] | None) -> dict[str, Any]:
        """Validate and normalize user-provided template inputs."""
        raw_inputs = inputs or {}
        normalized: dict[str, Any] = {}

        for field in self.fields:
            value = _normalize_value(raw_inputs.get(field.key, field.default))
            if field.required and _is_empty(value):
                raise TemplateValidationError(f"{field.label} is required for {self.name}.")

            if field.kind == "select" and not _is_empty(value):
                valid_values = {option.value for option in field.options}
                if str(value) not in valid_values:
                    raise TemplateValidationError(
                        f"{field.label} must be one of: "
                        f"{', '.join(option.label for option in field.options)}."
                    )

            if not _is_empty(value):
                normalized[field.key] = value

        return normalized

    def build_research_question(self, inputs: Mapping[str, Any]) -> str:
        """Render the template's default research question."""
        return self.question_builder(inputs).strip()

    def build_agent_guidance(self, inputs: Mapping[str, Any]) -> str:
        """Render methodology guidance for the discovery agent."""
        input_lines = []
        for field in self.fields:
            value = inputs.get(field.key)
            if _is_empty(value):
                continue
            input_lines.append(f"- {field.label}: {_format_value(value)}")

        sections = [
            f"## Template Guidance: {self.name}",
            (
                "The scientist selected this guided workflow. Treat the guardrails below "
                "as methodology requirements before freeform interpretation."
            ),
        ]

        if input_lines:
            sections.append("### Structured Inputs\n" + "\n".join(input_lines))

        if self.bundled_skills:
            sections.append(
                "### Bundled Skills\n"
                + _format_bullets(
                    f"`{skill.category}--{skill.slug}.md`: {skill.name}"
                    for skill in self.bundled_skills
                )
            )

        sections.extend(
            [
                "### Methodology Guardrails\n" + _format_bullets(self.methodology),
                "### Report Expectations\n" + _format_bullets(self.report_guidance),
            ]
        )
        if self.visualization_guidance:
            sections.append(
                "### Visualization Expectations\n" + _format_bullets(self.visualization_guidance)
            )

        return "\n\n".join(sections)


@dataclass(frozen=True)
class TemplateResolution:
    """Validated job template submission ready for persistence and runtime use."""

    template_id: str | None
    template_version: str | None
    template_inputs: dict[str, Any] | None
    research_question: str
    agent_guidance: str | None


def _normalize_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    text = str(value)
    return text if len(text) <= 240 else text[:237] + "..."


def _format_bullets(items: Iterable[str]) -> str:
    return "\n".join(f"- {item}" for item in items)
