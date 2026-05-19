"""YAML/plugin loader for guided job templates."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from openscientist.job_templates.builders import get_question_builder
from openscientist.job_templates.types import (
    JobTemplate,
    TemplateField,
    TemplateFieldKind,
    TemplateFieldOption,
    TemplateSkill,
    TemplateValidationError,
)

TEMPLATE_PATHS_ENV = "OPENSCIENTIST_TEMPLATE_PATHS"
BUILTIN_TEMPLATE_PACKAGE = "openscientist.job_templates.templates"
SUPPORTED_FIELD_KINDS: set[str] = {"text", "textarea", "select"}


def load_job_templates_from_paths(
    extra_paths: Sequence[Path] = (),
    *,
    include_builtins: bool = True,
) -> tuple[JobTemplate, ...]:
    """Load templates from packaged YAML and optional plugin paths.

    Plugin paths may be YAML files or directories containing `.yaml`/`.yml`
    files. Additional paths can also be supplied with
    `OPENSCIENTIST_TEMPLATE_PATHS`, using `os.pathsep` as the separator.
    """
    paths: list[Any] = []
    if include_builtins:
        paths.extend(_builtin_template_files())
    paths.extend(_external_template_files([*extra_paths, *_env_template_paths()]))

    templates = tuple(_load_template_file(path) for path in paths)
    _ensure_unique_template_ids(templates)
    return templates


def _builtin_template_files() -> list[Any]:
    template_root = resources.files(BUILTIN_TEMPLATE_PACKAGE)
    return sorted(
        (
            path
            for path in template_root.iterdir()
            if path.is_file() and path.name.endswith((".yaml", ".yml"))
        ),
        key=lambda path: path.name,
    )


def _env_template_paths() -> list[Path]:
    value = os.environ.get(TEMPLATE_PATHS_ENV, "")  # env-ok
    return [Path(part) for part in value.split(os.pathsep) if part.strip()]


def _external_template_files(paths: Sequence[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            raise TemplateValidationError(f"Template plugin path does not exist: {resolved}")
        if resolved.is_file():
            files.append(resolved)
            continue
        files.extend(sorted(resolved.glob("*.yaml")))
        files.extend(sorted(resolved.glob("*.yml")))
    return files


def _load_template_file(path: Any) -> JobTemplate:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TemplateValidationError(f"Template file must contain a YAML mapping: {path}")
    return _template_from_mapping(data, source_path=Path(str(path)))


def _template_from_mapping(data: Mapping[str, Any], *, source_path: Path) -> JobTemplate:
    question_builder = _build_question_builder(data, source_path)
    return JobTemplate(
        id=_required_string(data, "id", source_path),
        version=str(data.get("version", "1")),
        name=_required_string(data, "name", source_path),
        summary=_required_string(data, "summary", source_path),
        fields=tuple(
            _field_from_mapping(field, source_path) for field in _sequence(data, "fields")
        ),
        skill_slugs=tuple(_string_sequence(data, "skill_slugs")),
        skill_categories=tuple(_string_sequence(data, "skill_categories")),
        methodology=tuple(_string_sequence(data, "methodology")),
        report_guidance=tuple(_string_sequence(data, "report_guidance")),
        visualization_guidance=tuple(_string_sequence(data, "visualization_guidance")),
        question_builder=question_builder,
        default_max_iterations=int(data.get("default_max_iterations", 10)),
        bundled_skills=tuple(
            _template_skill_from_mapping(skill, source_path)
            for skill in _sequence(data, "bundled_skills", required=False)
        ),
    )


def _build_question_builder(
    data: Mapping[str, Any], source_path: Path
) -> Callable[[Mapping[str, Any]], str]:
    builder_id = data.get("question_builder")
    question_template = data.get("question_template")
    if builder_id and question_template:
        raise TemplateValidationError(
            f"Template {source_path} must use question_builder or question_template, not both."
        )
    if builder_id:
        try:
            return get_question_builder(str(builder_id))
        except KeyError as exc:
            raise TemplateValidationError(
                f"Unknown question_builder '{builder_id}' in {source_path}."
            ) from exc
    if isinstance(question_template, str) and question_template.strip():
        template = question_template.strip()
        return lambda inputs: template.format_map(_DefaultFormatMap(inputs))
    raise TemplateValidationError(
        f"Template {source_path} must define either question_builder or question_template."
    )


def _field_from_mapping(data: Any, source_path: Path) -> TemplateField:
    if not isinstance(data, dict):
        raise TemplateValidationError(f"Template field must be a mapping in {source_path}.")
    kind = str(data.get("kind", "text"))
    if kind not in SUPPORTED_FIELD_KINDS:
        raise TemplateValidationError(f"Unsupported field kind '{kind}' in {source_path}.")

    return TemplateField(
        key=_required_string(data, "key", source_path),
        label=_required_string(data, "label", source_path),
        kind=cast(TemplateFieldKind, kind),
        required=bool(data.get("required", False)),
        placeholder=str(data.get("placeholder", "")),
        help_text=str(data.get("help_text", "")),
        options=tuple(
            TemplateFieldOption(
                value=_required_string(option, "value", source_path),
                label=_required_string(option, "label", source_path),
            )
            for option in _sequence(data, "options", required=False)
        ),
        default=_optional_string(data.get("default")),
        rows=int(data.get("rows", 3)),
    )


def _template_skill_from_mapping(data: Any, source_path: Path) -> TemplateSkill:
    if not isinstance(data, dict):
        raise TemplateValidationError(f"Bundled skill must be a mapping in {source_path}.")

    if "content" in data:
        return TemplateSkill(
            name=_required_string(data, "name", source_path),
            category=_required_string(data, "category", source_path),
            slug=_required_string(data, "slug", source_path),
            description=str(data.get("description", "")),
            content=_required_string(data, "content", source_path),
        )

    skill_path = _resolve_skill_path(data, source_path)
    return _read_skill_file(skill_path)


def _resolve_skill_path(data: Mapping[str, Any], source_path: Path) -> Path:
    if "path" in data:
        path = Path(str(data["path"]))
        if not path.is_absolute():
            path = source_path.parent / path
        resolved = path.resolve()
        if not resolved.exists():
            raise TemplateValidationError(f"Bundled skill path does not exist: {resolved}")
        return resolved

    category = _required_string(data, "category", source_path)
    slug = _required_string(data, "slug", source_path)
    for root in _skill_roots(source_path):
        candidate = root / category / slug / "SKILL.md"
        if candidate.exists():
            return candidate
    raise TemplateValidationError(
        f"Could not find bundled skill {category}/{slug} referenced by {source_path}."
    )


def _skill_roots(source_path: Path) -> list[Path]:
    roots = [source_path.parent / "skills", _repo_root() / "skills"]
    return [root.resolve() for root in roots]


def _read_skill_file(path: Path) -> TemplateSkill:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        raise TemplateValidationError(f"Bundled skill is missing YAML frontmatter: {path}")
    frontmatter_raw, body = match.groups()
    metadata = yaml.safe_load(frontmatter_raw)
    if not isinstance(metadata, dict):
        raise TemplateValidationError(f"Bundled skill frontmatter must be a mapping: {path}")
    return TemplateSkill(
        name=str(metadata.get("name") or path.parent.name),
        category=str(metadata.get("category") or path.parent.parent.name),
        slug=str(metadata.get("slug") or path.parent.name),
        description=str(metadata.get("description") or ""),
        content=body.strip(),
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _ensure_unique_template_ids(templates: Sequence[JobTemplate]) -> None:
    seen: set[str] = set()
    for template in templates:
        if template.id in seen:
            raise TemplateValidationError(f"Duplicate job template id: {template.id}.")
        seen.add(template.id)


def _sequence(
    data: Mapping[str, Any],
    key: str,
    *,
    required: bool = True,
) -> Sequence[Any]:
    value = data.get(key)
    if value is None:
        if required:
            raise TemplateValidationError(f"Missing required template key: {key}.")
        return ()
    if not isinstance(value, list):
        raise TemplateValidationError(f"Template key '{key}' must be a list.")
    return value


def _string_sequence(data: Mapping[str, Any], key: str) -> list[str]:
    return [str(item) for item in _sequence(data, key, required=False)]


def _required_string(data: Mapping[str, Any], key: str, source_path: Path) -> str:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        raise TemplateValidationError(f"Missing required key '{key}' in {source_path}.")
    return str(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


class _DefaultFormatMap(dict[str, Any]):
    def __init__(self, values: Mapping[str, Any]) -> None:
        super().__init__(values)

    def __missing__(self, key: str) -> str:
        return ""
