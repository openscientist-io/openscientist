"""Built-in job templates for guided OpenScientist job creation.

Template selection is currently applied by inserting editable prompt text.
Persisting selected template IDs and skill constraints should use a dedicated
job metadata field in a follow-up instead of overloading provider config.
"""

from dataclasses import dataclass

FREEFORM_TEMPLATE_ID = "freeform"


@dataclass(frozen=True, slots=True)
class JobTemplate:
    """Prompt-shaping metadata for a built-in job template."""

    template_id: str
    name: str
    description: str
    prompt: str
    required_prompt_fields: tuple[str, ...] = ()
    recommended_skills: tuple[str, ...] = ()
    analysis_expectations: tuple[str, ...] = ()
    visualization_guidance: tuple[str, ...] = ()
    reporting_guidance: tuple[str, ...] = ()


GENE_SET_ENRICHMENT_TEMPLATE = JobTemplate(
    template_id="gene_set_enrichment",
    name="Gene Set Enrichment",
    description=(
        "Guides the agent to run deterministic enrichment statistics before "
        "interpreting a submitted gene set."
    ),
    required_prompt_fields=(
        "Gene set of interest",
        "Background gene set or measured gene universe",
        "Organism and gene identifier system",
        "Biological context or comparison, if known",
    ),
    recommended_skills=(
        "domain--genomics",
        "domain--data-science",
        "workflow--result-interpretation",
    ),
    analysis_expectations=(
        "Validate gene identifiers and report any unmapped or ambiguous IDs.",
        "Use deterministic enrichment statistics before biological interpretation.",
        "Use an explicit background gene universe, not all known genes by default.",
        "Apply multiple-testing correction and report adjusted p-values.",
        "Prefer GO enrichment for Gene Ontology questions; explain if another resource is used.",
    ),
    visualization_guidance=(
        "Produce a ranked enrichment table.",
        "Include a bar plot or dot plot of top enriched terms when results support it.",
    ),
    reporting_guidance=(
        "Report the background universe, ontology/resource, statistical test, and correction method.",
        "Separate computed enrichment results from interpretation and literature context.",
    ),
    prompt="""Template: Gene set enrichment analysis

Research goal:
- [Describe the biological question or comparison.]

Gene set of interest:
- [Paste gene symbols/IDs here, or reference an uploaded file and column.]

Background gene set or measured universe:
- [Paste the background genes, or describe exactly which genes were measured/testable.]

Organism and identifier system:
- [Example: human HGNC symbols, mouse MGI symbols, Ensembl IDs.]

Biological context:
- [Optional: disease, treatment, phenotype, tissue, assay, or comparison.]

Analysis expectations:
- Validate gene identifiers and report unmapped or ambiguous IDs.
- Run deterministic enrichment statistics before interpretation.
- Use the provided background gene universe for enrichment.
- Apply multiple-testing correction and report adjusted p-values.
- Prefer GO enrichment when the question is Gene Ontology focused; explain any alternate resource.
- Do not infer themes from memory before computing enrichment results.

Recommended OpenScientist skills:
- domain--genomics
- domain--data-science
- workflow--result-interpretation

Reporting and visualization preferences:
- Report the ontology/resource, statistical test, correction method, and significance threshold.
- Include a ranked enrichment table.
- Include a bar plot or dot plot of top enriched terms if useful.""",
)

_BUILT_IN_TEMPLATES: tuple[JobTemplate, ...] = (GENE_SET_ENRICHMENT_TEMPLATE,)
_TEMPLATES_BY_ID: dict[str, JobTemplate] = {
    template.template_id: template for template in _BUILT_IN_TEMPLATES
}


def list_job_templates() -> tuple[JobTemplate, ...]:
    """Return built-in job templates in display order."""
    return _BUILT_IN_TEMPLATES


def get_job_template(template_id: str | None) -> JobTemplate | None:
    """Return a built-in template by ID, or None for freeform/unknown IDs."""
    if template_id is None or template_id in ("", FREEFORM_TEMPLATE_ID):
        return None
    return _TEMPLATES_BY_ID.get(template_id)


def get_job_template_options() -> dict[str, str]:
    """Return select options for the job template dropdown."""
    return {
        FREEFORM_TEMPLATE_ID: "Freeform prompt",
        **{template.template_id: template.name for template in _BUILT_IN_TEMPLATES},
    }


def merge_template_prompt(current_prompt: str | None, template: JobTemplate) -> str:
    """Insert template prompt text while preserving any freeform user notes."""
    current = (current_prompt or "").strip()
    template_prompt = template.prompt.strip()

    if not current:
        return template_prompt
    if template_prompt in current:
        return current

    return f"{template_prompt}\n\nAdditional freeform notes:\n{current}"
