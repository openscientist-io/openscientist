"""Tests for guided job templates."""

from types import SimpleNamespace

import pytest

from openscientist.job_templates import (
    FREEFORM_DEFAULT_MAX_ITERATIONS,
    TemplateValidationError,
    default_max_iterations_for_template,
    filter_skills_for_template,
    get_job_template,
    list_job_templates,
    resolve_template_submission,
)


def test_registry_includes_initial_guided_workflows() -> None:
    """The first template set should cover several distinct scientific job shapes."""
    template_ids = {template.id for template in list_job_templates()}

    assert {
        "gene-set-enrichment",
        "evidence-synthesis",
        "computational-simulation",
        "microbiome-differential-abundance",
        "variant-interpretation",
    }.issubset(template_ids)


def test_templates_define_workflow_default_iterations() -> None:
    """Guided workflows should suggest focused iteration counts for the form."""
    template_defaults = {
        template.id: template.default_max_iterations for template in list_job_templates()
    }

    assert template_defaults["gene-set-enrichment"] == 2
    assert template_defaults["variant-interpretation"] == 3
    assert all(2 <= value <= 10 for value in template_defaults.values())
    assert default_max_iterations_for_template("freeform") == FREEFORM_DEFAULT_MAX_ITERATIONS
    assert default_max_iterations_for_template("gene-set-enrichment") == 2


def test_variant_template_generates_question_and_guidance() -> None:
    """Variant interpretation can be submitted without a hand-written question."""
    resolution = resolve_template_submission(
        template_id="variant-interpretation",
        template_inputs={
            "variant_input": "uploaded family.vcf",
            "organism_build": "human-grch38",
            "phenotype_context": "neurodevelopmental delay with seizures",
            "interpretation_mode": "diagnostic",
            "inheritance_model": "de-novo",
        },
        research_question="",
    )

    assert resolution.template_id == "variant-interpretation"
    assert resolution.template_version == "1"
    assert "uploaded family.vcf" in resolution.research_question
    assert "deterministic sources" in resolution.research_question
    assert resolution.agent_guidance is not None
    assert "Rank variants using explicit evidence" in resolution.agent_guidance


def test_gene_set_template_bundles_enrichment_and_visualization_skills() -> None:
    """Enrichment jobs should carry concrete skills even before DB skill sync."""
    template = get_job_template("gene-set-enrichment")
    assert template is not None

    bundled_keys = {(skill.category, skill.slug) for skill in template.bundled_skills}

    assert ("domain", "ontology-enrichment") in bundled_keys
    assert ("workflow", "scientific-visualization") in bundled_keys

    resolution = resolve_template_submission(
        template_id="gene-set-enrichment",
        template_inputs={
            "gene_set_label": "interferon stimulated genes",
            "foreground_genes": "IFIT1\nISG15\nMX1",
            "organism": "Homo sapiens",
            "database": "go",
        },
        research_question="",
    )
    assert resolution.agent_guidance is not None
    assert "`domain--ontology-enrichment.md`: Ontology Enrichment Analysis" in (
        resolution.agent_guidance
    )


def test_guided_template_validates_required_fields() -> None:
    """Templates should block missing inputs that determine the scientific workflow."""
    with pytest.raises(TemplateValidationError, match="Phenotype or disease context"):
        resolve_template_submission(
            template_id="variant-interpretation",
            template_inputs={
                "variant_input": "chr1-123-A-G",
                "organism_build": "human-grch38",
                "interpretation_mode": "exploratory",
            },
            research_question="",
        )


def test_freeform_requires_research_question() -> None:
    """Freeform mode preserves the existing explicit prompt requirement."""
    with pytest.raises(TemplateValidationError, match="research question"):
        resolve_template_submission(
            template_id=None,
            template_inputs=None,
            research_question="",
        )


def test_template_skill_filter_keeps_workflow_and_matching_domain_skills() -> None:
    """Guided jobs should preload relevant domain skills without dropping workflow skills."""
    skills = [
        SimpleNamespace(category="workflow", slug="result-interpretation", name="Result"),
        SimpleNamespace(category="domain", slug="genomics", name="Genomics"),
        SimpleNamespace(category="domain", slug="metabolomics", name="Metabolomics"),
    ]

    filtered = filter_skills_for_template(skills, "variant-interpretation")

    assert [(skill.category, skill.slug) for skill in filtered] == [
        ("workflow", "result-interpretation"),
        ("domain", "genomics"),
        ("workflow", "scientific-visualization"),
    ]


def test_template_skill_filter_adds_bundled_skills_and_deduplicates_existing() -> None:
    """Template bundles should be guaranteed but not duplicate DB-synced skills."""
    skills = [
        SimpleNamespace(category="workflow", slug="result-interpretation", name="Result"),
        SimpleNamespace(category="domain", slug="ontology-enrichment", name="DB Ontology"),
    ]

    filtered = filter_skills_for_template(skills, "gene-set-enrichment")
    keys = [(skill.category, skill.slug) for skill in filtered]

    assert keys == [
        ("workflow", "result-interpretation"),
        ("domain", "ontology-enrichment"),
        ("workflow", "scientific-visualization"),
    ]
    assert filtered[1].name == "DB Ontology"


def test_freeform_skill_filter_preserves_all_skills() -> None:
    """Freeform jobs should keep the previous all-enabled-skills behavior."""
    skills = [
        SimpleNamespace(category="workflow", slug="result-interpretation", name="Result"),
        SimpleNamespace(category="domain", slug="metabolomics", name="Metabolomics"),
    ]

    assert filter_skills_for_template(skills, None) == skills
