"""Tests for built-in job template registry."""

from openscientist.job_templates import (
    FREEFORM_TEMPLATE_ID,
    GENE_SET_ENRICHMENT_TEMPLATE,
    get_job_template,
    get_job_template_options,
    list_job_templates,
    merge_template_prompt,
)


def test_registry_includes_gene_set_enrichment_template():
    """The initial template slice should include concrete scientific guidance."""
    templates = list_job_templates()

    assert GENE_SET_ENRICHMENT_TEMPLATE in templates
    assert get_job_template("gene_set_enrichment") == GENE_SET_ENRICHMENT_TEMPLATE


def test_freeform_template_id_does_not_resolve_to_template():
    """Freeform remains the default blank prompt path."""
    assert get_job_template(FREEFORM_TEMPLATE_ID) is None
    assert get_job_template(None) is None


def test_template_options_include_freeform_first():
    """The dropdown should preserve freeform as the first/default option."""
    options = get_job_template_options()

    assert list(options.items())[0] == (FREEFORM_TEMPLATE_ID, "Freeform prompt")
    assert options["gene_set_enrichment"] == "Gene Set Enrichment"


def test_gene_set_template_requires_background_gene_set():
    """Gene set enrichment guidance should push for best-practice inputs."""
    template = GENE_SET_ENRICHMENT_TEMPLATE

    assert "Background gene set or measured gene universe" in template.required_prompt_fields
    assert "domain--genomics" in template.recommended_skills
    assert "Run deterministic enrichment statistics before interpretation." in template.prompt
    assert "Use an explicit background gene universe, not all known genes by default." in (
        template.analysis_expectations
    )


def test_merge_template_prompt_prefills_empty_prompt():
    """Applying a template to an empty prompt should return the template text."""
    merged = merge_template_prompt("", GENE_SET_ENRICHMENT_TEMPLATE)

    assert merged.startswith("Template: Gene set enrichment analysis")
    assert "Background gene set or measured universe" in merged


def test_merge_template_prompt_preserves_freeform_notes():
    """Applying a template should retain existing user-written prompt text."""
    merged = merge_template_prompt(
        "Focus on immune signaling and explain negative results.",
        GENE_SET_ENRICHMENT_TEMPLATE,
    )

    assert merged.startswith("Template: Gene set enrichment analysis")
    assert "Additional freeform notes:" in merged
    assert "Focus on immune signaling and explain negative results." in merged


def test_merge_template_prompt_is_idempotent():
    """Repeated template insertions should not duplicate template text."""
    first = merge_template_prompt("", GENE_SET_ENRICHMENT_TEMPLATE)
    second = merge_template_prompt(first, GENE_SET_ENRICHMENT_TEMPLATE)

    assert second == first
