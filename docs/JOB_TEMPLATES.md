# Guided Job Templates

Guided job templates are YAML specs loaded by `openscientist.job_templates`.
They are intended to be data-first and plugin-friendly: Python is only needed
when a template needs custom question-generation logic.

## Built-In Templates

Built-ins live in:

```text
src/openscientist/job_templates/templates/
```

Each YAML file defines:

- `id`, `version`, `name`, `summary`
- `default_max_iterations`
- `fields` rendered on `/new`
- `skill_slugs` and `skill_categories` used to select existing enabled skills
- `bundled_skills` guaranteed to be written to `.claude/skills/`
- `methodology`, `report_guidance`, and `visualization_guidance`
- either `question_template` or `question_builder`

Use `question_template` for simple no-code templates:

```yaml
id: literature-gap
version: "1"
name: Literature Gap
summary: Find gaps in a specified literature area.
default_max_iterations: 2
question_template: "Find open research gaps in {topic} for {organism}."
fields:
  - key: topic
    label: Topic
    required: true
  - key: organism
    label: Organism
skill_slugs: []
skill_categories:
  - workflow
methodology:
  - Separate established findings from open questions.
report_guidance:
  - Include a ranked list of gaps and supporting evidence.
visualization_guidance: []
```

Use `question_builder` only when wording needs conditional logic. Builder hooks
are registered in `src/openscientist/job_templates/builders.py`.

## Plugin Paths

Additional template packs can be loaded without changing Python code by setting
`OPENSCIENTIST_TEMPLATE_PATHS` to one or more YAML files or directories. Use the
platform path separator (`:` on macOS/Linux, `;` on Windows).

```bash
export OPENSCIENTIST_TEMPLATE_PATHS="/path/to/templates:/path/to/one-template.yaml"
```

Template IDs must be unique across built-ins and plugin paths.

## Bundled Skills

Templates can guarantee job-local skills even before database skill sync has
run:

```yaml
bundled_skills:
  - category: domain
    slug: ontology-enrichment
  - category: workflow
    slug: scientific-visualization
```

The loader resolves these against the repository `skills/` directory and writes
them into `.claude/skills/` for matching guided jobs. A plugin template may also
reference a relative skill file path or provide inline `content`.
